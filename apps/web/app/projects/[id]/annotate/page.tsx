"use client";

import { use } from "react";
import dynamic from "next/dynamic";
import { ReactQueryProvider } from "@/lib/query-client";

const AnnotateClient = dynamic(() => import("./AnnotateClient"), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-screen text-zinc-500 text-sm">
      Loading canvas…
    </div>
  ),
});

export default function AnnotatePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return (
    <ReactQueryProvider>
      <AnnotateClient projectId={id} />
    </ReactQueryProvider>
  );
}
