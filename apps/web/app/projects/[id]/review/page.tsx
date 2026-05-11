"use client";

import { useParams } from "next/navigation";
import { ReactQueryProvider } from "@/lib/query-client";
import ReviewClient from "./ReviewClient";

export default function ReviewPage() {
  const { id: projectId } = useParams<{ id: string }>();
  return (
    <ReactQueryProvider>
      <ReviewClient projectId={projectId} />
    </ReactQueryProvider>
  );
}
