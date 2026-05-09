"use client";
import { useRouter, useParams } from "next/navigation";
import { Layers, ChevronRight } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";

export default function ReviewPageWrapper() {
  return (
    <ReactQueryProvider>
      <ReviewPage />
    </ReactQueryProvider>
  );
}

function ReviewPage() {
  const { id: projectId } = useParams<{ id: string }>();
  const router = useRouter();
  return (
    <div className="min-h-screen bg-zinc-50">
      <header className="border-b border-zinc-200 bg-white px-8 py-4 flex items-center gap-3">
        <button onClick={() => router.push("/")} className="flex items-center gap-2 text-zinc-500 hover:text-zinc-800 text-sm">
          <Layers size={18} />EasyLab
        </button>
        <ChevronRight size={14} className="text-zinc-300" />
        <button onClick={() => router.push(`/projects/${projectId}`)} className="text-sm text-zinc-500 hover:text-zinc-800">Project</button>
        <ChevronRight size={14} className="text-zinc-300" />
        <span className="text-sm font-medium text-zinc-900">Review</span>
      </header>
      <main className="max-w-4xl mx-auto px-8 py-16 text-center">
        <p className="text-zinc-400 text-sm">Review table — coming in Phase 3.</p>
      </main>
    </div>
  );
}
