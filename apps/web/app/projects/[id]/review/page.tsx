"use client";
import { useParams } from "next/navigation";
import { Table2 } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";
import { Shell, Card, PageTitle } from "@/components/Shell";

export default function ReviewPageWrapper() {
  return (
    <ReactQueryProvider>
      <ReviewPage />
    </ReactQueryProvider>
  );
}

function ReviewPage() {
  const { id: projectId } = useParams<{ id: string }>();
  return (
    <Shell
      crumbs={[
        { label: "Project", href: `/projects/${projectId}` },
        { label: "Review" },
      ]}
    >
      <PageTitle title="Review" description="Inspect AI-extracted values and fix any mistakes." />
      <Card className="p-14 flex flex-col items-center text-center gap-4">
        <div className="grid place-items-center w-12 h-12 rounded-2xl bg-gradient-to-br from-indigo-500/15 to-violet-500/15 text-indigo-600 dark:text-indigo-300">
          <Table2 size={22} />
        </div>
        <p className="text-sm text-[var(--muted)] max-w-sm">
          The review table lives on a different branch (Phase 4–5). It hasn&apos;t been merged into
          this branch yet — see the chat for instructions.
        </p>
      </Card>
    </Shell>
  );
}
