"use client";

import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Check, Clock, ChevronRight, Layers } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";

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
  href: (id: string) => string;
  done: (p: Project) => boolean;
  accessible: (p: Project) => boolean;
};

const STEPS: Step[] = [
  {
    label: "Upload",
    href: (id) => `/projects/${id}/upload`,
    done: (p) => (p.image_count ?? 0) >= 1 && p.reference_image_id !== null,
    accessible: () => true,
  },
  {
    label: "Annotate",
    href: (id) => `/projects/${id}/annotate`,
    done: (p) => (p.field_count ?? 0) >= 1,
    accessible: (p) => (p.image_count ?? 0) >= 1 && p.reference_image_id !== null,
  },
  {
    label: "Process",
    href: (id) => `/projects/${id}/process`,
    done: (p) => p.status === "done",
    accessible: (p) => (p.field_count ?? 0) >= 1,
  },
  {
    label: "Review",
    href: (id) => `/projects/${id}/review`,
    done: () => false,
    accessible: (p) => p.status === "done",
  },
  {
    label: "Export",
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
      <div className="min-h-screen bg-zinc-50 flex items-center justify-center">
        <p className="text-sm text-zinc-400">Loading…</p>
      </div>
    );
  }

  if (isError || !project) {
    return (
      <div className="min-h-screen bg-zinc-50 flex items-center justify-center">
        <p className="text-sm text-red-500">Project not found.</p>
      </div>
    );
  }

  const currentStep = STEPS.findIndex((s) => !s.done(project));

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
        <span className="text-sm font-medium text-zinc-900">{project.name}</span>
      </header>

      <main className="max-w-2xl mx-auto px-8 py-12">
        <h2 className="text-xl font-semibold text-zinc-900 mb-8">{project.name}</h2>

        <ol className="space-y-3">
          {STEPS.map((step, i) => {
            const done = step.done(project);
            const accessible = step.accessible(project);
            const isCurrent = i === currentStep;

            return (
              <li key={step.label}>
                <button
                  disabled={!accessible}
                  onClick={() => accessible && router.push(step.href(id))}
                  className={[
                    "w-full flex items-center gap-4 p-4 rounded-lg border text-left transition-colors",
                    done
                      ? "border-green-200 bg-green-50 hover:bg-green-100"
                      : isCurrent && accessible
                      ? "border-blue-300 bg-blue-50 hover:bg-blue-100"
                      : accessible
                      ? "border-zinc-200 bg-white hover:bg-zinc-50"
                      : "border-zinc-100 bg-zinc-50 opacity-50 cursor-not-allowed",
                  ].join(" ")}
                >
                  <div
                    className={[
                      "w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 text-sm font-semibold",
                      done
                        ? "bg-green-500 text-white"
                        : isCurrent && accessible
                        ? "bg-blue-500 text-white"
                        : "bg-zinc-200 text-zinc-500",
                    ].join(" ")}
                  >
                    {done ? <Check size={16} /> : isCurrent ? <Clock size={16} /> : i + 1}
                  </div>
                  <span
                    className={[
                      "font-medium",
                      done
                        ? "text-green-800"
                        : isCurrent
                        ? "text-blue-800"
                        : "text-zinc-700",
                    ].join(" ")}
                  >
                    {step.label}
                  </span>
                  {accessible && <ChevronRight size={16} className="ml-auto text-zinc-300" />}
                </button>
              </li>
            );
          })}
        </ol>
      </main>
    </div>
  );
}
