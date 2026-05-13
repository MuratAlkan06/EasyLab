"use client";

import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Check, ArrowRight, Upload, Pencil, Cpu, Table2, Download } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";
import { Shell, Card, PageTitle, Spinner } from "@/components/Shell";

type Project = {
  id: string;
  name: string;
  status: "draft" | "annotated" | "processing" | "done";
  reference_image_id: string | null;
  image_count: number;
  field_count: number;
  has_active_job: boolean;
  latest_job_id: string | null;
};

type Step = {
  label: string;
  description: string;
  icon: LucideIcon;
  href: (id: string) => string;
  done: (p: Project) => boolean;
  accessible: (p: Project) => boolean;
};

const STEPS: Step[] = [
  {
    label: "Upload",
    description: "Drop in 10–50 similar images and pick a reference",
    icon: Upload,
    href: (id) => `/projects/${id}/upload`,
    done: (p) => (p.image_count ?? 0) >= 1 && p.reference_image_id !== null,
    accessible: () => true,
  },
  {
    label: "Annotate",
    description: "Draw rectangles around each field on the reference",
    icon: Pencil,
    href: (id) => `/projects/${id}/annotate`,
    done: (p) => (p.field_count ?? 0) >= 1,
    accessible: (p) => (p.image_count ?? 0) >= 1 && p.reference_image_id !== null,
  },
  {
    label: "Process",
    description: "Run AI extraction across the whole batch",
    icon: Cpu,
    href: (id) => `/projects/${id}/process`,
    done: (p) => p.status === "done",
    accessible: (p) => (p.field_count ?? 0) >= 1,
  },
  {
    label: "Review",
    description: "Inspect each value, fix what the AI missed",
    icon: Table2,
    href: (id) => `/projects/${id}/review`,
    done: () => false,
    accessible: (p) => p.status === "done",
  },
  {
    label: "Export",
    description: "Download the cleaned data as CSV",
    icon: Download,
    href: (id) => `/projects/${id}/export`,
    done: () => false,
    accessible: (p) => p.status === "done",
  },
];

export default function ProjectHubPage() {
  return (
    <ReactQueryProvider>
      <ProjectHub />
    </ReactQueryProvider>
  );
}

function ProjectHub() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();

  const { data: project, isLoading, isError } = useQuery({
    queryKey: ["project", id],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${id}`);
      if (!res.ok) throw new Error("Project not found");
      return res.json() as Promise<Project>;
    },
  });

  if (isLoading) {
    return (
      <Shell>
        <div className="flex items-center gap-2 text-[var(--muted)] text-sm">
          <Spinner /> Loading project…
        </div>
      </Shell>
    );
  }

  if (isError || !project) {
    return (
      <Shell>
        <Card className="p-8 text-center text-sm text-red-600">Project not found.</Card>
      </Shell>
    );
  }

  const currentStep = STEPS.findIndex((s) => !s.done(project));
  const completed = STEPS.filter((s) => s.done(project)).length;
  const pct = Math.round((completed / STEPS.length) * 100);

  return (
    <Shell crumbs={[{ label: project.name }]} contentClassName="max-w-3xl">
      <PageTitle
        title={project.name}
        description={`${completed} of ${STEPS.length} steps complete`}
      />

      <div className="mb-6 h-1.5 rounded-full bg-[var(--surface-muted)] overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-indigo-500 to-violet-600 transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>

      <ol className="space-y-2.5">
        {STEPS.map((step, i) => {
          const done = step.done(project);
          const accessible = step.accessible(project);
          const isCurrent = i === currentStep;
          const Icon = step.icon;

          return (
            <li key={step.label}>
              <button
                disabled={!accessible}
                onClick={() => accessible && router.push(step.href(id))}
                className={[
                  "group w-full flex items-center gap-4 p-4 rounded-xl border text-left transition-all",
                  done
                    ? "border-emerald-200/70 bg-emerald-50/60 dark:border-emerald-400/20 dark:bg-emerald-500/5 hover:bg-emerald-50"
                    : isCurrent && accessible
                    ? "border-[var(--primary)] bg-[var(--primary-soft)] shadow-sm shadow-indigo-500/10 hover:shadow-md hover:shadow-indigo-500/20"
                    : accessible
                    ? "border-[var(--border)] bg-[var(--surface)] hover:border-[var(--border-strong)] hover:-translate-y-px hover:shadow-sm"
                    : "border-[var(--border)] bg-[var(--surface-muted)]/60 opacity-50 cursor-not-allowed",
                ].join(" ")}
              >
                <div
                  className={[
                    "grid place-items-center w-10 h-10 rounded-lg flex-shrink-0 text-sm font-semibold transition-colors",
                    done
                      ? "bg-emerald-500 text-white"
                      : isCurrent && accessible
                      ? "bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-sm shadow-indigo-500/30"
                      : "bg-[var(--surface-muted)] text-[var(--muted)] border border-[var(--border)]",
                  ].join(" ")}
                >
                  {done ? <Check size={18} strokeWidth={3} /> : <Icon size={18} />}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-mono text-[var(--subtle)]">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span
                      className={[
                        "font-semibold",
                        done
                          ? "text-emerald-800 dark:text-emerald-300"
                          : isCurrent
                          ? "text-[var(--foreground)]"
                          : "text-[var(--foreground)]",
                      ].join(" ")}
                    >
                      {step.label}
                    </span>
                  </div>
                  <p className="text-xs text-[var(--muted)] mt-0.5">{step.description}</p>
                </div>
                {accessible && (
                  <ArrowRight
                    size={16}
                    className="text-[var(--subtle)] group-hover:text-[var(--primary)] group-hover:translate-x-0.5 transition-all flex-shrink-0"
                  />
                )}
              </button>
            </li>
          );
        })}
      </ol>
    </Shell>
  );
}
