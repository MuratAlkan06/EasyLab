"use client";

import { startTransition, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, X, Clock, Loader2, AlertTriangle, Cpu, Play } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";
import { Shell, Button, Card, PageTitle, Badge } from "@/components/Shell";

type Field = { id: string; field_name: string };
type Task = {
  image_id: string;
  filename: string;
  status: "pending" | "detecting" | "extracting" | "done" | "failed" | "not_found";
  error: string | null;
  updated_at: string;
};
type Job = {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress_total: number;
  progress_done: number;
  attempts: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

const TASK_STATUS_ICON: Record<Task["status"], React.ReactNode> = {
  pending: <Clock size={14} className="text-[var(--subtle)]" />,
  detecting: <div className="w-3 h-3 rounded-full bg-blue-400 animate-pulse" />,
  extracting: <Loader2 size={14} className="text-blue-500 animate-spin" />,
  done: <Check size={14} className="text-emerald-500" strokeWidth={3} />,
  failed: <X size={14} className="text-red-500" strokeWidth={3} />,
  not_found: <span className="text-[var(--subtle)] text-xs">–</span>,
};

const TASK_STATUS_TONE: Record<Task["status"], "neutral" | "blue" | "green" | "red" | "amber"> = {
  pending: "neutral",
  detecting: "blue",
  extracting: "blue",
  done: "green",
  failed: "red",
  not_found: "amber",
};

export default function ProcessPageWrapper() {
  return (
    <ReactQueryProvider>
      <ProcessPage />
    </ReactQueryProvider>
  );
}

function ProcessPage() {
  const { id: projectId } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const redirectedRef = useRef(false);

  const { data: fieldsData } = useQuery({
    queryKey: ["fields", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/fields`);
      if (!res.ok) throw new Error("Failed to load fields");
      return res.json() as Promise<{ fields: Field[] }>;
    },
  });

  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}`);
      if (!res.ok) throw new Error("Project not found");
      return res.json() as Promise<{
        id: string;
        has_active_job: boolean;
        latest_job_id: string | null;
        status: string;
        template_quality: string | null;
      }>;
    },
  });

  useEffect(() => {
    if (project && !project.has_active_job) {
      if (project.latest_job_id) {
        startTransition(() => setActiveJobId(project.latest_job_id));
      } else {
        startTransition(() => setShowModal(true));
      }
    } else if (project?.has_active_job && project.latest_job_id) {
      startTransition(() => setActiveJobId(project.latest_job_id));
    }
  }, [project]);

  const { data: jobData } = useQuery({
    queryKey: ["job", projectId, activeJobId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/jobs/${activeJobId}`);
      if (!res.ok) throw new Error("Failed to load job");
      return res.json() as Promise<{ job: Job; tasks: Task[] }>;
    },
    enabled: !!activeJobId,
    refetchInterval: (query) => {
      const status = query.state.data?.job?.status;
      if (status === "succeeded" || status === "failed" || status === "cancelled") return false;
      return 1500;
    },
  });

  const job = jobData?.job;
  const tasks = useMemo(() => jobData?.tasks ?? [], [jobData]);

  useEffect(() => {
    if (job && job.status !== "succeeded" && job.status !== "failed" && job.status !== "cancelled") {
      document.title = `(${job.progress_done}/${job.progress_total}) EasyLab — Processing`;
    } else {
      document.title = "EasyLab";
    }
    return () => {
      document.title = "EasyLab";
    };
  }, [job]);

  useEffect(() => {
    if (!job || redirectedRef.current) return;
    if (job.status === "succeeded") {
      redirectedRef.current = true;
      const failedCount = tasks.filter((t) => t.status === "failed").length;
      if (failedCount === 0) {
        toast.success(`Processing complete — ${job.progress_done} images extracted`);
        setTimeout(() => router.push(`/projects/${projectId}/review`), 1500);
      } else {
        toast.warning(`Processing done — ${job.progress_done} succeeded, ${failedCount} failed`);
      }
    } else if (job.status === "failed") {
      toast.error(`Processing failed — ${job.error ?? "Unknown error"}`);
    }
  }, [job, tasks, projectId, router]);

  const startJob = useMutation({
    mutationFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/jobs`, { method: "POST" });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error?.message ?? "Failed to start job");
      return body as { job: Job };
    },
    onSuccess: (data) => {
      setShowModal(false);
      setActiveJobId(data.job.id);
      qc.invalidateQueries({ queryKey: ["project", projectId] });
      toast.info(`Processing started — ${data.job.progress_total} images queued`);
    },
    onError: (e: Error) => {
      setShowModal(false);
      toast.error(e.message);
    },
  });

  const cancelJob = useMutation({
    mutationFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/jobs/${activeJobId}`, { method: "DELETE" });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error?.message ?? "Failed to cancel job");
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["job", projectId, activeJobId] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const fields = fieldsData?.fields ?? [];
  const doneCount = tasks.filter((t) => t.status === "done").length;
  const failedCount = tasks.filter((t) => t.status === "failed").length;
  const pendingCount = tasks.filter((t) => t.status === "pending").length;
  const inProgressCount = tasks.filter(
    (t) => t.status === "detecting" || t.status === "extracting",
  ).length;
  const progressPct =
    (job?.progress_total ?? 0) > 0
      ? Math.min(100, Math.round(((job?.progress_done ?? 0) / (job?.progress_total ?? 1)) * 100))
      : 0;

  const jobStatusTone: Record<Job["status"], "blue" | "amber" | "green" | "red" | "neutral"> = {
    queued: "neutral",
    running: "blue",
    succeeded: "green",
    failed: "red",
    cancelled: "amber",
  };

  return (
    <Shell
      crumbs={[
        { label: "Project", href: `/projects/${projectId}` },
        { label: "Process" },
      ]}
      contentClassName="max-w-4xl"
    >
      <PageTitle
        title="Processing"
        description={
          job
            ? `Job ${job.id.slice(0, 8)} — ${job.progress_done}/${job.progress_total} images`
            : "Run AI extraction across every uploaded image."
        }
        rightSlot={job ? <Badge tone={jobStatusTone[job.status]}>{job.status}</Badge> : undefined}
      />

      {showModal && (
        <ConfirmModal
          fields={fields}
          onCancel={() => router.push(`/projects/${projectId}`)}
          onStart={() => startJob.mutate()}
          loading={startJob.isPending}
        />
      )}

      {project?.template_quality === "degraded" && (
        <Card className="mb-5 p-4 flex items-start gap-3 border-amber-200 bg-amber-50/70 dark:bg-amber-500/5 dark:border-amber-400/20">
          <AlertTriangle size={18} className="text-amber-600 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-amber-800 dark:text-amber-200">
            Template quality is degraded — Gemini could not analyse the reference image. Extraction
            accuracy may be lower.
          </p>
        </Card>
      )}

      {job ? (
        <>
          <Card className="p-5 space-y-4 mb-5">
            <div className="flex items-center justify-between gap-4 text-sm flex-wrap">
              <div className="flex items-center gap-3 flex-wrap">
                <Stat label="Done" value={doneCount} tone="green" />
                <Stat label="In progress" value={inProgressCount} tone="blue" />
                <Stat label="Failed" value={failedCount} tone="red" />
                <Stat label="Pending" value={pendingCount} tone="neutral" />
              </div>
              <span className="font-mono text-sm font-semibold text-[var(--foreground)]">
                {progressPct}%
              </span>
            </div>
            <div className="w-full bg-[var(--surface-muted)] rounded-full h-2 overflow-hidden">
              <div
                className="bg-gradient-to-r from-indigo-500 to-violet-600 h-2 rounded-full transition-all duration-500"
                style={{ width: `${progressPct}%` }}
              />
            </div>
            {job.status === "running" && (
              <div className="flex justify-end">
                <button
                  onClick={() => cancelJob.mutate()}
                  disabled={cancelJob.isPending}
                  className="text-xs text-[var(--muted)] hover:text-red-500 transition-colors"
                >
                  Cancel job
                </button>
              </div>
            )}
          </Card>

          {job.status === "succeeded" && failedCount > 0 && (
            <Card className="mb-5 p-4 flex items-start gap-3 border-amber-200 bg-amber-50/70 dark:bg-amber-500/5 dark:border-amber-400/20">
              <AlertTriangle size={18} className="text-amber-600 flex-shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-amber-900 dark:text-amber-200">
                  {doneCount} succeeded · {failedCount} failed
                </p>
                <p className="text-xs text-amber-700 dark:text-amber-300/80 mt-0.5">
                  Some fields were not found. You can fix them manually in Review.
                </p>
              </div>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => router.push(`/projects/${projectId}/review`)}
              >
                Go to Review
              </Button>
            </Card>
          )}

          {tasks.length > 0 && (
            <Card className="overflow-hidden">
              <ul className="divide-y divide-[var(--border)] max-h-[60vh] overflow-y-auto">
                {tasks.map((task) => (
                  <li
                    key={task.image_id}
                    className="flex items-center gap-3 px-4 py-2.5 hover:bg-[var(--surface-muted)] transition-colors"
                  >
                    <div className="w-5 h-5 grid place-items-center flex-shrink-0">
                      {TASK_STATUS_ICON[task.status]}
                    </div>
                    <span className="text-sm text-[var(--foreground)] truncate flex-1 min-w-0">
                      {task.filename}
                    </span>
                    <Badge tone={TASK_STATUS_TONE[task.status]}>
                      {task.status.replace("_", " ")}
                    </Badge>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      ) : (
        <Card className="p-12 text-center">
          {startJob.isPending ? (
            <div className="flex items-center justify-center gap-2 text-[var(--muted)] text-sm">
              <Loader2 size={16} className="animate-spin" /> Starting job…
            </div>
          ) : (
            <div className="flex flex-col items-center gap-4">
              <div className="grid place-items-center w-12 h-12 rounded-2xl bg-gradient-to-br from-indigo-500/15 to-violet-500/15 text-indigo-600 dark:text-indigo-300">
                <Cpu size={22} />
              </div>
              <p className="text-sm text-[var(--muted)]">No active job for this project.</p>
              <Button onClick={() => setShowModal(true)}>
                <Play size={14} fill="currentColor" /> Start processing
              </Button>
            </div>
          )}
        </Card>
      )}
    </Shell>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "neutral" | "green" | "red" | "blue";
}) {
  const colors: Record<typeof tone, string> = {
    neutral: "text-[var(--muted)]",
    green: "text-emerald-600 dark:text-emerald-400",
    red: "text-red-600 dark:text-red-400",
    blue: "text-blue-600 dark:text-blue-400",
  };
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span className={`font-semibold tabular-nums ${colors[tone]}`}>{value}</span>
      <span className="text-[var(--muted)]">{label}</span>
    </span>
  );
}

function ConfirmModal({
  fields,
  onCancel,
  onStart,
  loading,
}: {
  fields: Field[];
  onCancel: () => void;
  onStart: () => void;
  loading: boolean;
}) {
  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <Card className="max-w-md w-full p-6 space-y-5">
        <div>
          <h3 className="text-lg font-semibold text-[var(--foreground)]">
            Process {fields.length} field{fields.length !== 1 ? "s" : ""}?
          </h3>
          <p className="text-sm text-[var(--muted)] mt-1">
            EasyLab will detect and extract these fields across every uploaded image.
          </p>
        </div>
        <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-muted)] p-3">
          <p className="text-xs uppercase tracking-wide text-[var(--subtle)] font-semibold mb-2">
            Fields
          </p>
          <ul className="space-y-1.5">
            {fields.slice(0, 6).map((f) => (
              <li
                key={f.id}
                className="text-sm text-[var(--foreground)] flex items-center gap-2"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--primary)] flex-shrink-0" />
                <span className="font-mono text-xs">{f.field_name}</span>
              </li>
            ))}
            {fields.length > 6 && (
              <li className="text-xs text-[var(--muted)] pl-3.5">+ {fields.length - 6} more</li>
            )}
          </ul>
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button onClick={onStart} loading={loading}>
            <Play size={14} fill="currentColor" /> Start processing
          </Button>
        </div>
      </Card>
    </div>
  );
}
