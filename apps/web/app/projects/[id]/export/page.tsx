"use client";
import { useParams } from "next/navigation";
import { Download } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";
import { Shell, Card, PageTitle } from "@/components/Shell";

export default function ExportPageWrapper() {
  return (
    <ReactQueryProvider>
      <ExportPage />
    </ReactQueryProvider>
  );
}

function ExportPage() {
  const { id: projectId } = useParams<{ id: string }>();
  return (
    <Shell
      crumbs={[
        { label: "Project", href: `/projects/${projectId}` },
        { label: "Export" },
      ]}
    >
      <PageTitle title="Export" description="Download your reviewed data as CSV." />
      <Card className="p-14 flex flex-col items-center text-center gap-4">
        <div className="grid place-items-center w-12 h-12 rounded-2xl bg-gradient-to-br from-indigo-500/15 to-violet-500/15 text-indigo-600 dark:text-indigo-300">
          <Download size={22} />
        </div>
        <p className="text-sm text-[var(--muted)] max-w-sm">
          CSV export ships with the review table on a different branch. Not yet merged into this
          branch.
        </p>
      </Card>
    </Shell>
  );
}
