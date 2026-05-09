"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2, ChevronRight, Layers } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";

type Project = {
  id: string;
  name: string;
  status: "draft" | "annotated" | "processing" | "done";
  image_count: number;
  reference_image_id: string | null;
  created_at: string;
  updated_at: string;
};

const STATUS_LABEL: Record<Project["status"], string> = {
  draft: "Draft",
  annotated: "Annotated",
  processing: "Processing",
  done: "Done",
};

const STATUS_COLOR: Record<Project["status"], string> = {
  draft: "bg-zinc-100 text-zinc-500",
  annotated: "bg-blue-100 text-blue-600",
  processing: "bg-yellow-100 text-yellow-700",
  done: "bg-green-100 text-green-700",
};

export default function RootPage() {
  return (
    <ReactQueryProvider>
      <Dashboard />
    </ReactQueryProvider>
  );
}

function Dashboard() {
  const router = useRouter();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const res = await fetch("/api/projects");
      if (!res.ok) throw new Error("Failed to load projects");
      return res.json() as Promise<{ projects: Project[] }>;
    },
  });

  const createProject = useMutation({
    mutationFn: async (name: string) => {
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error?.message ?? "Failed to create project");
      return body as { project: Project };
    },
    onSuccess: ({ project }) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setCreating(false);
      setNewName("");
      router.push(`/projects/${project.id}/upload`);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const deleteProject = useMutation({
    mutationFn: async (id: string) => {
      const res = await fetch(`/api/projects/${id}`, { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.error?.message ?? "Failed to delete project");
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setDeleteConfirm(null);
      toast.success("Project deleted");
    },
    onError: (e: Error) => {
      setDeleteConfirm(null);
      toast.error(e.message);
    },
  });

  const projects = data?.projects ?? [];

  function handleCreateSubmit(e: React.FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (name) createProject.mutate(name);
  }

  return (
    <div className="min-h-screen bg-zinc-50">
      <header className="border-b border-zinc-200 bg-white px-8 py-4 flex items-center gap-3">
        <Layers className="text-zinc-700" size={22} />
        <h1 className="text-lg font-semibold text-zinc-900">EasyLab</h1>
      </header>

      <main className="max-w-4xl mx-auto px-8 py-10 space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl font-semibold text-zinc-900">Projects</h2>
            <p className="text-sm text-zinc-500 mt-0.5">
              Upload lab images, annotate fields, and extract data with AI.
            </p>
          </div>
          {!creating && (
            <button
              onClick={() => setCreating(true)}
              className="flex items-center gap-2 px-4 py-2 bg-zinc-900 text-white text-sm rounded-lg hover:bg-zinc-700 transition-colors"
            >
              <Plus size={16} />
              New Project
            </button>
          )}
        </div>

        {creating && (
          <form
            onSubmit={handleCreateSubmit}
            className="bg-white border border-zinc-200 rounded-lg p-4 flex gap-2"
          >
            <input
              autoFocus
              className="flex-1 border border-zinc-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-400"
              placeholder="Project name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              maxLength={120}
            />
            <button
              type="submit"
              disabled={!newName.trim() || createProject.isPending}
              className="px-4 py-2 bg-zinc-900 text-white text-sm rounded-md hover:bg-zinc-700 disabled:opacity-50 transition-colors"
            >
              {createProject.isPending ? "Creating…" : "Create"}
            </button>
            <button
              type="button"
              onClick={() => { setCreating(false); setNewName(""); }}
              className="px-3 py-2 text-sm text-zinc-500 hover:text-zinc-800"
            >
              Cancel
            </button>
          </form>
        )}

        {isLoading ? (
          <div className="bg-white border border-zinc-200 rounded-lg p-8 text-center text-sm text-zinc-400">
            Loading…
          </div>
        ) : projects.length === 0 ? (
          <div className="bg-white border border-zinc-200 rounded-lg p-12 flex flex-col items-center gap-4 text-center">
            <Layers size={40} className="text-zinc-300" />
            <div>
              <p className="font-medium text-zinc-700">No projects yet</p>
              <p className="text-sm text-zinc-400 mt-1">
                Create a project to start extracting data from your lab images.
              </p>
            </div>
            <button
              onClick={() => setCreating(true)}
              className="flex items-center gap-2 px-4 py-2 bg-zinc-900 text-white text-sm rounded-lg hover:bg-zinc-700 transition-colors"
            >
              <Plus size={16} />
              New Project
            </button>
          </div>
        ) : (
          <div className="bg-white border border-zinc-200 rounded-lg divide-y divide-zinc-100">
            {projects.map((p) => (
              <div key={p.id} className="flex items-center px-5 py-4 hover:bg-zinc-50 transition-colors">
                <button
                  className="flex-1 text-left flex items-center gap-4 min-w-0"
                  onClick={() => router.push(`/projects/${p.id}`)}
                >
                  <div className="min-w-0">
                    <p className="font-medium text-zinc-900 truncate">{p.name}</p>
                    <p className="text-xs text-zinc-400 mt-0.5">
                      {p.image_count} image{p.image_count !== 1 ? "s" : ""}
                      {" · "}
                      {new Date(p.updated_at).toLocaleDateString()}
                    </p>
                  </div>
                </button>
                <div className="flex items-center gap-3 ml-4">
                  <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${STATUS_COLOR[p.status]}`}>
                    {STATUS_LABEL[p.status]}
                  </span>
                  {deleteConfirm === p.id ? (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-zinc-500">Delete?</span>
                      <button
                        onClick={() => deleteProject.mutate(p.id)}
                        className="text-xs text-red-600 hover:text-red-800 font-medium"
                      >
                        Yes
                      </button>
                      <button
                        onClick={() => setDeleteConfirm(null)}
                        className="text-xs text-zinc-400 hover:text-zinc-600"
                      >
                        No
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={(e) => { e.stopPropagation(); setDeleteConfirm(p.id); }}
                      className="p-1.5 text-zinc-300 hover:text-red-400 transition-colors"
                    >
                      <Trash2 size={15} />
                    </button>
                  )}
                  <ChevronRight size={16} className="text-zinc-300" />
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
