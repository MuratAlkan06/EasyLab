"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2, ArrowRight, FlaskConical, ImageIcon, X } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";
import { Shell, Button, Card, Badge, PageTitle } from "@/components/Shell";

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

const STATUS_TONE: Record<Project["status"], "neutral" | "blue" | "amber" | "green"> = {
  draft: "neutral",
  annotated: "blue",
  processing: "amber",
  done: "green",
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
    <Shell>
      <PageTitle
        title="Projects"
        description="Upload similar lab images, label fields once, and let AI extract the same data across the whole batch."
        rightSlot={
          !creating ? (
            <Button onClick={() => setCreating(true)}>
              <Plus size={16} strokeWidth={2.4} /> New project
            </Button>
          ) : null
        }
      />

      {creating && (
        <Card className="mb-5 p-4">
          <form onSubmit={handleCreateSubmit} className="flex flex-wrap gap-2 items-center">
            <input
              autoFocus
              className="flex-1 min-w-[220px] h-10 px-3 rounded-lg bg-[var(--surface-muted)] border border-[var(--border-strong)] text-sm placeholder:text-[var(--subtle)] focus:outline-none focus:border-[var(--primary)] focus:bg-[var(--surface)] transition-colors"
              placeholder="Project name (e.g. Multimeter readings — Lab 3)"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              maxLength={120}
            />
            <Button type="submit" disabled={!newName.trim()} loading={createProject.isPending}>
              Create
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setCreating(false);
                setNewName("");
              }}
            >
              Cancel
            </Button>
          </form>
        </Card>
      )}

      {isLoading ? (
        <Card className="p-10 text-center text-sm text-[var(--subtle)]">Loading projects…</Card>
      ) : projects.length === 0 ? (
        <Card className="p-14 flex flex-col items-center text-center gap-5">
          <div className="grid place-items-center w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500/15 to-violet-500/15 text-indigo-600 dark:text-indigo-300">
            <FlaskConical size={26} />
          </div>
          <div className="space-y-1">
            <p className="font-semibold text-[var(--foreground)]">Start your first project</p>
            <p className="text-sm text-[var(--muted)] max-w-sm">
              Upload a batch of similar images, label one as a reference, and EasyLab will extract the
              same fields from the rest.
            </p>
          </div>
          {!creating && (
            <Button onClick={() => setCreating(true)}>
              <Plus size={16} strokeWidth={2.4} /> New project
            </Button>
          )}
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <ul className="divide-y divide-[var(--border)]">
            {projects.map((p) => (
              <li
                key={p.id}
                className="group flex items-center px-5 py-4 hover:bg-[var(--surface-muted)] transition-colors"
              >
                <button
                  className="flex-1 text-left flex items-center gap-3 min-w-0"
                  onClick={() => router.push(`/projects/${p.id}`)}
                >
                  <div className="grid place-items-center w-9 h-9 rounded-lg bg-[var(--surface-muted)] border border-[var(--border)] text-[var(--muted)] group-hover:text-[var(--primary)] group-hover:border-[var(--primary-soft)] transition-colors flex-shrink-0">
                    <ImageIcon size={16} />
                  </div>
                  <div className="min-w-0">
                    <p className="font-medium text-[var(--foreground)] truncate">{p.name}</p>
                    <p className="text-xs text-[var(--subtle)] mt-0.5">
                      {p.image_count} image{p.image_count !== 1 ? "s" : ""} · updated{" "}
                      {new Date(p.updated_at).toLocaleDateString(undefined, {
                        month: "short",
                        day: "numeric",
                      })}
                    </p>
                  </div>
                </button>
                <div className="flex items-center gap-3 ml-4">
                  <Badge tone={STATUS_TONE[p.status]}>{STATUS_LABEL[p.status]}</Badge>
                  {deleteConfirm === p.id ? (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-[var(--muted)]">Delete?</span>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => deleteProject.mutate(p.id)}
                        loading={deleteProject.isPending}
                      >
                        Delete
                      </Button>
                      <button
                        onClick={() => setDeleteConfirm(null)}
                        className="p-1.5 text-[var(--subtle)] hover:text-[var(--foreground)]"
                        aria-label="Cancel"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteConfirm(p.id);
                      }}
                      className="p-1.5 rounded-md text-[var(--subtle)] hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 transition-colors opacity-0 group-hover:opacity-100"
                      aria-label="Delete project"
                    >
                      <Trash2 size={15} />
                    </button>
                  )}
                  <ArrowRight
                    size={16}
                    className="text-[var(--subtle)] group-hover:text-[var(--primary)] group-hover:translate-x-0.5 transition-all"
                  />
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </Shell>
  );
}
