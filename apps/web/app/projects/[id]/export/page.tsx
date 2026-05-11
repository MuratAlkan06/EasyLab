"use client";

import { useCallback, useState } from "react";
import { useRouter, useParams } from "next/navigation";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChevronRight, Download, Layers, Loader2 } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";

type ProjectResponse = {
  id: string;
  name: string;
  image_count: number;
  field_count: number;
  latest_job_id: string | null;
};

export default function ExportPageWrapper() {
  return (
    <ReactQueryProvider>
      <ExportPage />
    </ReactQueryProvider>
  );
}

function ExportPage() {
  const { id: projectId } = useParams<{ id: string }>();
  const router = useRouter();

  const projectQuery = useQuery({
    queryKey: ["project", projectId],
    queryFn: async (): Promise<ProjectResponse> => {
      const res = await fetch(`/api/projects/${projectId}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? "Failed to load project");
      }
      return res.json();
    },
  });

  const [downloading, setDownloading] = useState(false);

  const handleDownload = useCallback(async () => {
    setDownloading(true);
    try {
      const res = await fetch(`/api/projects/${projectId}/export.csv`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? `Export failed (${res.status})`);
      }

      const blob = await res.blob();

      // Prefer server-provided filename from Content-Disposition; fall back to a sensible default.
      const disposition = res.headers.get("Content-Disposition") ?? "";
      const match = disposition.match(/filename="?([^"]+)"?/i);
      const suggested = match?.[1] ?? `easylab-${projectId}.csv`;

      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = suggested;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);

      toast.success("CSV ready");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }, [projectId]);

  const project = projectQuery.data;
  const noJob = !!project && project.latest_job_id === null;

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
        <span className="text-sm font-medium text-zinc-900">Export</span>
      </header>

      <main className="max-w-2xl mx-auto px-8 py-16">
        {projectQuery.isLoading ? (
          <div className="flex items-center justify-center text-zinc-500 text-sm">
            <Loader2 size={16} className="animate-spin mr-2" />
            Loading…
          </div>
        ) : projectQuery.isError ? (
          <div className="text-center text-red-600 text-sm">
            {projectQuery.error instanceof Error
              ? projectQuery.error.message
              : "Failed to load"}
          </div>
        ) : !project ? null : (
          <div className="bg-white border border-zinc-200 rounded-lg p-8 space-y-6">
            <div className="space-y-1">
              <h1 className="text-xl font-semibold text-zinc-900">
                Export {project.name}
              </h1>
              <p className="text-sm text-zinc-500">
                Download the extracted values as a CSV file.
              </p>
            </div>

            <dl className="grid grid-cols-2 gap-4 text-sm border-t border-zinc-100 pt-6">
              <div>
                <dt className="text-xs uppercase tracking-wide text-zinc-400">
                  Images
                </dt>
                <dd className="mt-1 text-zinc-900 tabular-nums">
                  {project.image_count}
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-zinc-400">
                  Fields
                </dt>
                <dd className="mt-1 text-zinc-900 tabular-nums">
                  {project.field_count}
                </dd>
              </div>
            </dl>

            <div className="border-t border-zinc-100 pt-6 space-y-3">
              <p className="text-xs text-zinc-500 leading-relaxed">
                The CSV contains one row per image. For each field there are three
                columns: value, confidence (0–1), and status. Cells you corrected
                use your value; cells that failed extraction show
                <code className="px-1 mx-1 rounded bg-zinc-100 text-[11px]">[FAILED]</code>.
                Cells that need review have a
                <code className="px-1 mx-1 rounded bg-zinc-100 text-[11px]">*</code>
                after the confidence number.
              </p>

              {noJob ? (
                <div className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded px-3 py-2">
                  No completed job yet. Process your images first.
                </div>
              ) : (
                <button
                  onClick={handleDownload}
                  disabled={downloading}
                  className="inline-flex items-center gap-2 bg-zinc-900 text-white text-sm font-medium px-4 py-2 rounded hover:bg-zinc-800 disabled:opacity-60 disabled:cursor-not-allowed"
                >
                  {downloading ? (
                    <>
                      <Loader2 size={14} className="animate-spin" />
                      Preparing…
                    </>
                  ) : (
                    <>
                      <Download size={14} />
                      Download CSV
                    </>
                  )}
                </button>
              )}

              <div className="pt-2">
                <Link
                  href={`/projects/${projectId}/review`}
                  className="text-xs text-zinc-500 hover:text-zinc-800"
                >
                  ← Back to review
                </Link>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
