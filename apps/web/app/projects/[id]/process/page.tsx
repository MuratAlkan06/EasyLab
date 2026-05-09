"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, X, Clock, Loader2, ChevronRight, Layers, AlertTriangle } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";

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
  pending: <Clock size={16} className="text-zinc-400" />,
  detecting: <div className="w-4 h-4 rounded-full bg-blue-400 animate-pulse" />,
  extracting: <Loader2 size={16} className="text-blue-500 animate-spin" />,
  done: <Check size={16} className="text-green-500" />,
  failed: <X size={16} className="text-red-500" />,
  not_found: <span className="text-zinc-400 text-sm">–</span>,
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

  // Load fields (for modal preview)
  const { data: fieldsData } = useQuery({
    queryKey: ["fields", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/fields`);
      if (!res.ok) throw new Error("Failed to load fields");
      return res.json() as Promise<{ fields: Field[] }>;
    },
  });

  // Load project to find existing active job
  const { data: project } = useQuery({
    queryKey: ["project", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}`);
      if (!res.ok) throw new Error("Project not found");
      return res.json() as Promise<{ id: string; has_active_job: boolean; latest_job_id: string | null; status: string; template_quality: string | null }>;
    },
  });

  // Show modal on load if no active job
  useEffect(() => {
    if (project && !project.has_active_job) {
      if (project.latest_job_id) {
        setActiveJobId(project.latest_job_id);
      } else {
        setShowModal(true);
      }
    } else if (project?.has_active_job && project.latest_job_id) {
      setActiveJobId(project.latest_job_id);
    }
  }, [project]);

  // Poll job status
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
  const tasks = jobData?.tasks ?? [];

  // Handle job completion
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
      const res = await fetch(`/api/projects/${projectId}/jobs/${activeJobId}`, {
        method: "DELETE",
      });
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
  const progressPct =
    (job?.progress_total ?? 0) > 0
      ? Math.round(((job?.progress_done ?? 0) / (job?.progress_total ?? 1)) * 100)
      : 0;

  return (
    <div className="min-h-screen bg-zinc-50">
      <header className="border-b border-zinc-200 bg-white px-8 py-4 flex items-center gap-3">
        <button
          onClick={() => router.push("/")}
          className="flex items-center gap-2 text-zinc-500 hover:text-zinc-800 text-sm"
        >
          <Layers size={18} />
          EasyLab
        </button>
        <ChevronRight size={14} className="text-zinc-300" />
        <button
          onClick={() => router.push(`/projects/${projectId}`)}
          className="text-sm text-zinc-500 hover:text-zinc-800"
        >
          Project
        </button>
        <ChevronRight size={14} className="text-zinc-300" />
        <span className="text-sm font-medium text-zinc-900">Process</span>
      </header>

      {/* Confirmation modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl p-6 max-w-sm w-full mx-4 space-y-4">
            <h3 className="text-lg font-semibold text-zinc-900">
              Ready to process {fields.length} field{fields.length !== 1 ? "s" : ""}?
            </h3>
            <div className="space-y-1">
              <p className="text-sm text-zinc-500 font-medium">Fields to extract:</p>
              <ul className="space-y-1">
                {fields.slice(0, 5).map((f) => (
                  <li key={f.id} className="text-sm text-zinc-700 flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-zinc-400 flex-shrink-0" />
                    {f.field_name}
                  </li>
                ))}
                {fields.length > 5 && (
                  <li className="text-sm text-zinc-400">+ {fields.length - 5} more</li>
                )}
              </ul>
            </div>
            <div className="flex gap-3 justify-end pt-2">
              <button
                onClick={() => router.push(`/projects/${projectId}`)}
                className="px-4 py-2 text-sm text-zinc-600 hover:text-zinc-900"
              >
                Cancel
              </button>
              <button
                onClick={() => startJob.mutate()}
                disabled={startJob.isPending}
                className="flex items-center gap-2 px-4 py-2 bg-zinc-900 text-white text-sm rounded-lg hover:bg-zinc-700 disabled:opacity-50 transition-colors"
              >
                {startJob.isPending && <Loader2 size={14} className="animate-spin" />}
                Start Processing
              </button>
            </div>
          </div>
        </div>
      )}

      <main className="max-w-3xl mx-auto px-8 py-10 space-y-6">
        <h2 className="text-xl font-semibold text-zinc-900">Processing</h2>

        {project?.template_quality === "degraded" && (
          <div className="flex items-start gap-3 bg-yellow-50 border border-yellow-200 rounded-lg p-4">
            <AlertTriangle size={18} className="text-yellow-600 flex-shrink-0 mt-0.5" />
            <p className="text-sm text-yellow-800">
              Template quality is degraded — Gemini could not analyse the reference image.
              Extraction accuracy may be lower.
            </p>
          </div>
        )}

        {job ? (
          <>
            {/* Progress bar */}
            <div className="bg-white border border-zinc-200 rounded-lg p-5 space-y-3">
              <div className="flex items-center justify-between text-sm">
                <span className="text-zinc-500">
                  {doneCount} done · {failedCount} failed · {pendingCount} pending
                </span>
                <span className="font-medium text-zinc-700">{progressPct}%</span>
              </div>
              <div className="w-full bg-zinc-100 rounded-full h-2">
                <div
                  className="bg-zinc-900 h-2 rounded-full transition-all duration-500"
                  style={{ width: `${progressPct}%` }}
                />
              </div>
              {job.status === "running" && (
                <div className="flex justify-end">
                  <button
                    onClick={() => cancelJob.mutate()}
                    disabled={cancelJob.isPending}
                    className="text-xs text-zinc-400 hover:text-red-500 transition-colors"
                  >
                    Cancel job
                  </button>
                </div>
              )}
            </div>

            {/* Partial failure banner */}
            {job.status === "succeeded" && failedCount > 0 && (
              <div className="flex items-start gap-3 bg-yellow-50 border border-yellow-200 rounded-lg p-4">
                <AlertTriangle size={18} className="text-yellow-600 flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-yellow-800">
                    {doneCount} succeeded · {failedCount} failed — some fields were not found
                  </p>
                </div>
                <button
                  onClick={() => router.push(`/projects/${projectId}/review`)}
                  className="text-sm text-yellow-700 font-medium hover:text-yellow-900 whitespace-nowrap"
                >
                  Go to Review
                </button>
              </div>
            )}

            {/* Image task list */}
            {tasks.length > 0 && (
              <div className="bg-white border border-zinc-200 rounded-lg divide-y divide-zinc-100">
                {tasks.map((task) => (
                  <div key={task.image_id} className="flex items-center gap-3 px-5 py-3">
                    <div className="w-5 h-5 flex items-center justify-center flex-shrink-0">
                      {TASK_STATUS_ICON[task.status]}
                    </div>
                    <span className="text-sm text-zinc-700 truncate flex-1 min-w-0">
                      {task.filename}
                    </span>
                    <span className="text-xs text-zinc-400 capitalize">{task.status}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <div className="bg-white border border-zinc-200 rounded-lg p-8 text-center text-sm text-zinc-400">
            {startJob.isPending ? (
              <div className="flex items-center justify-center gap-2">
                <Loader2 size={16} className="animate-spin" />
                Starting job…
              </div>
            ) : (
              <button
                onClick={() => setShowModal(true)}
                className="text-zinc-600 hover:text-zinc-900"
              >
                Click to start processing
              </button>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
