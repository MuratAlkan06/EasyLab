"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, CheckCircle2, FileSpreadsheet } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";
import { Shell, Button, Card, PageTitle, Spinner, Badge } from "@/components/Shell";

type Project = {
  id: string;
  name: string;
  status: "draft" | "annotated" | "processing" | "done";
  image_count: number;
  field_count: number;
  has_active_job: boolean;
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
  const [downloading, setDownloading] = useState(false);

  const { data: project, isLoading } = useQuery({
    queryKey: ["project", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}`);
      if (!res.ok) throw new Error("Project not found");
      return (await res.json()) as Project;
    },
  });

  async function handleDownload() {
    if (downloading) return;
    setDownloading(true);
    try {
      const res = await fetch(`/api/projects/${projectId}/export.csv`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? "Failed to download CSV");
      }

      const disposition = res.headers.get("Content-Disposition") ?? "";
      const match = /filename="([^"]+)"/.exec(disposition);
      const filename = match?.[1] ?? `easylab-${projectId}.csv`;

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.success("CSV downloaded");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  const ready = project?.status === "done" || !!project?.latest_job_id;

  return (
    <Shell
      crumbs={[
        { label: "Project", href: `/projects/${projectId}` },
        { label: "Export" },
      ]}
      contentClassName="max-w-2xl"
    >
      <PageTitle
        title="Export"
        description="Download every extracted value as a CSV. Cell overrides are baked in."
        rightSlot={project ? <Badge tone="indigo">{project.name}</Badge> : undefined}
      />

      <Card className="p-8 flex flex-col items-center text-center gap-5">
        <div className="grid place-items-center w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500/15 to-violet-500/15 text-indigo-600 dark:text-indigo-300">
          <FileSpreadsheet size={26} />
        </div>

        {isLoading ? (
          <p className="text-sm text-[var(--muted)] flex items-center gap-2">
            <Spinner /> Loading project…
          </p>
        ) : !ready ? (
          <p className="text-sm text-[var(--muted)] max-w-sm">
            No completed job yet. Process your images first, then come back to download the CSV.
          </p>
        ) : (
          <>
            <div>
              <p className="text-[15px] font-medium text-[var(--foreground)]">
                Ready to download
              </p>
              <p className="text-xs text-[var(--muted)] mt-1 max-w-md">
                Columns: <code className="font-mono">filename</code>, then for each field{" "}
                <code className="font-mono">value</code>,{" "}
                <code className="font-mono">_confidence</code>,{" "}
                <code className="font-mono">_status</code>. Failed cells show{" "}
                <code className="font-mono">[FAILED]</code>; needs-review cells get a{" "}
                <code className="font-mono">*</code> on confidence.
              </p>
            </div>
            <Button onClick={handleDownload} loading={downloading} size="lg">
              <Download size={16} strokeWidth={2.4} />
              {downloading ? "Preparing CSV…" : "Download CSV"}
            </Button>
          </>
        )}
      </Card>

      <ul className="mt-6 grid gap-2 text-sm text-[var(--muted)]">
        <li className="flex items-center gap-2">
          <CheckCircle2 size={14} className="text-emerald-500 flex-shrink-0" />
          UTF-8 with BOM so Excel autodetects encoding
        </li>
        <li className="flex items-center gap-2">
          <CheckCircle2 size={14} className="text-emerald-500 flex-shrink-0" />
          Cell overrides from the review table replace the raw AI value
        </li>
        <li className="flex items-center gap-2">
          <CheckCircle2 size={14} className="text-emerald-500 flex-shrink-0" />
          Re-run a job and your overrides are still applied
        </li>
      </ul>
    </Shell>
  );
}
