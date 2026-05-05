"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient, QueryClient, QueryClientProvider } from "@tanstack/react-query";

const queryClient = new QueryClient();

export default function RootPage() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  );
}

type Job = {
  id: string;
  status: string;
  kind: string;
  progress_total: number;
  progress_done: number;
  error: string | null;
  created_at: string;
};

type Project = {
  id: string;
  name: string;
  status: string;
  created_at: string;
};

function Dashboard() {
  const qc = useQueryClient();
  const [newName, setNewName] = useState("");
  const [activeJob, setActiveJob] = useState<{ projectId: string; jobId: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data: projectsData } = useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const res = await fetch("/api/projects");
      return res.json() as Promise<{ projects: Project[] }>;
    },
  });

  const { data: jobData } = useQuery({
    queryKey: ["job", activeJob?.projectId, activeJob?.jobId],
    queryFn: async () => {
      const res = await fetch(
        `/api/projects/${activeJob!.projectId}/jobs/${activeJob!.jobId}`
      );
      return res.json() as Promise<{ job: Job }>;
    },
    enabled: !!activeJob,
    refetchInterval: (query) => {
      const status = query.state.data?.job?.status;
      if (status === "succeeded" || status === "failed" || status === "cancelled") return false;
      return 1500;
    },
  });

  const createProject = useMutation({
    mutationFn: async (name: string) => {
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.error?.message ?? "Failed to create project");
      }
      return res.json() as Promise<{ project: Project }>;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setNewName("");
      setError(null);
    },
    onError: (e: Error) => setError(e.message),
  });

  const enqueueJob = useMutation({
    mutationFn: async (projectId: string) => {
      const res = await fetch(`/api/projects/${projectId}/jobs`, {
        method: "POST",
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.error?.message ?? "Failed to enqueue job");
      }
      return res.json() as Promise<{ job: Job }>;
    },
    onSuccess: (data, projectId) => {
      setActiveJob({ projectId, jobId: data.job.id });
      setError(null);
    },
    onError: (e: Error) => setError(e.message),
  });

  const job = jobData?.job;

  return (
    <div className="min-h-screen bg-zinc-50 p-8 font-sans">
      <div className="max-w-2xl mx-auto space-y-8">
        <h1 className="text-2xl font-semibold text-zinc-900">EasyLab — Phase 1 Smoke Test</h1>

        {/* Create project */}
        <section className="bg-white rounded-lg border border-zinc-200 p-6 space-y-4">
          <h2 className="font-medium text-zinc-800">Create Project</h2>
          <div className="flex gap-2">
            <input
              className="flex-1 border border-zinc-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-400"
              placeholder="Project name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && newName.trim()) {
                  createProject.mutate(newName.trim());
                }
              }}
            />
            <button
              className="px-4 py-2 bg-zinc-900 text-white text-sm rounded hover:bg-zinc-700 disabled:opacity-50"
              disabled={!newName.trim() || createProject.isPending}
              onClick={() => createProject.mutate(newName.trim())}
            >
              {createProject.isPending ? "Creating…" : "Create"}
            </button>
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
        </section>

        {/* Projects list */}
        <section className="bg-white rounded-lg border border-zinc-200 divide-y divide-zinc-100">
          {!projectsData?.projects?.length ? (
            <p className="p-6 text-sm text-zinc-400">No projects yet.</p>
          ) : (
            projectsData.projects.map((p) => (
              <div key={p.id} className="p-4 flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-zinc-900">{p.name}</p>
                  <p className="text-xs text-zinc-400">{p.id}</p>
                </div>
                <button
                  className="px-3 py-1.5 text-xs text-zinc-800 bg-zinc-100 hover:bg-zinc-200 rounded disabled:opacity-50"
                  disabled={enqueueJob.isPending}
                  onClick={() => enqueueJob.mutate(p.id)}
                >
                  Run fake job
                </button>
              </div>
            ))
          )}
        </section>

        {/* Active job progress */}
        {activeJob && job && (
          <section className="bg-white rounded-lg border border-zinc-200 p-6 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="font-medium text-zinc-800">Active Job</h2>
              <StatusBadge status={job.status} />
            </div>
            <p className="text-xs text-zinc-400 font-mono">{job.id}</p>

            <div className="space-y-1">
              <div className="flex justify-between text-xs text-zinc-500">
                <span>Progress</span>
                <span>{job.progress_done} / {job.progress_total}</span>
              </div>
              <div className="w-full bg-zinc-100 rounded-full h-2">
                <div
                  className="bg-zinc-900 h-2 rounded-full transition-all duration-500"
                  style={{
                    width: job.progress_total > 0
                      ? `${(job.progress_done / job.progress_total) * 100}%`
                      : "0%",
                  }}
                />
              </div>
            </div>

            {job.error && (
              <p className="text-xs text-red-600 bg-red-50 rounded p-2">{job.error}</p>
            )}

            {(job.status === "succeeded" || job.status === "failed" || job.status === "cancelled") && (
              <button
                className="text-xs text-zinc-400 hover:text-zinc-600 underline"
                onClick={() => setActiveJob(null)}
              >
                Dismiss
              </button>
            )}
          </section>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    queued:    "bg-zinc-100 text-zinc-600",
    running:   "bg-blue-100 text-blue-700",
    succeeded: "bg-green-100 text-green-700",
    failed:    "bg-red-100 text-red-700",
    cancelled: "bg-zinc-100 text-zinc-400",
  };
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${styles[status] ?? "bg-zinc-100 text-zinc-600"}`}>
      {status}
    </span>
  );
}
