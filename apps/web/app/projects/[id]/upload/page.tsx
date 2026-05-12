"use client";

import { startTransition, useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useDropzone } from "react-dropzone";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Upload, CheckCircle, X, Star, ArrowRight, ImageIcon } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";
import { Shell, Button, PageTitle, Badge } from "@/components/Shell";

type ImageItem = {
  id: string;
  filename: string;
  status: string;
  is_reference: boolean;
  signed_url: string | null;
  localUrl?: string;
  uploading?: boolean;
  uploadProgress?: number;
};

export default function UploadPageWrapper() {
  return (
    <ReactQueryProvider>
      <UploadPage />
    </ReactQueryProvider>
  );
}

function UploadPage() {
  const { id: projectId } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const [images, setImages] = useState<ImageItem[]>([]);
  const [referenceId, setReferenceId] = useState<string | null>(null);
  const [settingRef, setSettingRef] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const { data: existingImages } = useQuery({
    queryKey: ["images", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/images`);
      if (!res.ok) throw new Error("Failed to load images");
      return res.json() as Promise<{ images: ImageItem[] }>;
    },
  });

  useEffect(() => {
    if (!existingImages?.images) return;
    const fromServer = existingImages.images.filter((i) => i.status !== "pending_upload");
    startTransition(() => {
      setImages((prev) => {
        // Preserve any locally tracked failed/uploading rows that the server
        // doesn't know about (failed confirms stay as 'pending_upload' on the
        // server). Otherwise users would lose the visible 'failed' marker on
        // the next refetch and not know why the upload didn't take.
        const serverIds = new Set(fromServer.map((i) => i.id));
        const localOnly = prev.filter(
          (i) => !serverIds.has(i.id) && (i.status === "failed" || i.uploading),
        );
        return [...fromServer, ...localOnly];
      });
      const ref = fromServer.find((i) => i.is_reference);
      if (ref) setReferenceId(ref.id);
    });
  }, [existingImages]);

  const uploadFiles = useCallback(
    async (files: File[]) => {
      const validFiles = files.filter((f) => {
        if (!["image/jpeg", "image/png"].includes(f.type)) {
          toast.error(`${f.name}: only JPG and PNG are supported`);
          return false;
        }
        if (f.size > 20 * 1024 * 1024) {
          toast.error(`${f.name}: file too large (max 20 MB)`);
          return false;
        }
        return true;
      });
      if (!validFiles.length) return;

      let urlData: {
        uploads: { image_id: string; upload_url: string; token: string; filename: string; storage_path: string }[];
      };
      try {
        const res = await fetch(`/api/projects/${projectId}/upload-urls`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            files: validFiles.map((f) => ({
              filename: f.name,
              mime_type: f.type,
              size_bytes: f.size,
            })),
          }),
        });
        const body = await res.json();
        if (!res.ok) throw new Error(body.error?.message ?? "Failed to get upload URLs");
        urlData = body;
      } catch (e: unknown) {
        toast.error(e instanceof Error ? e.message : "Upload failed");
        return;
      }

      const placeholders: ImageItem[] = urlData.uploads.map((u, i) => ({
        id: u.image_id,
        filename: u.filename,
        status: "pending_upload",
        is_reference: false,
        signed_url: null,
        localUrl: URL.createObjectURL(validFiles[i]),
        uploading: true,
        uploadProgress: 0,
      }));
      setImages((prev) => [...prev, ...placeholders]);

      await Promise.all(
        urlData.uploads.map(async (upload, i) => {
          try {
            const putRes = await fetch(upload.upload_url, {
              method: "PUT",
              headers: { "Content-Type": validFiles[i].type },
              body: validFiles[i],
            });
            if (!putRes.ok) {
              throw new Error(`Storage upload failed (${putRes.status})`);
            }

            const confirmRes = await fetch(
              `/api/projects/${projectId}/images/${upload.image_id}/confirm`,
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ size_bytes: validFiles[i].size }),
              },
            );
            if (!confirmRes.ok) {
              const body = await confirmRes.json().catch(() => ({}));
              throw new Error(
                body?.error?.message ?? `Validation failed (${confirmRes.status})`,
              );
            }

            if (!mountedRef.current) return;
            setImages((prev) =>
              prev.map((img) =>
                img.id === upload.image_id ? { ...img, status: "uploaded", uploading: false } : img,
              ),
            );
          } catch (e: unknown) {
            if (!mountedRef.current) return;
            setImages((prev) =>
              prev.map((img) =>
                img.id === upload.image_id ? { ...img, uploading: false, status: "failed" } : img,
              ),
            );
            const reason = e instanceof Error ? e.message : "Upload failed";
            toast.error(`${upload.filename}: ${reason}`);
          }
        }),
      );

      qc.invalidateQueries({ queryKey: ["images", projectId] });
    },
    [projectId, qc],
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop: uploadFiles,
    onDropRejected: (rejections) => {
      // react-dropzone's accept matcher dropped these silently before. Surface
      // every reason so the user knows why a file didn't upload.
      for (const r of rejections) {
        const reason = r.errors[0]?.message ?? "rejected";
        toast.error(`${r.file.name}: ${reason}`);
      }
    },
    accept: { "image/jpeg": [], "image/png": [] },
    multiple: true,
    noClick: images.length > 0,
  });

  async function handleSetReference(imageId: string) {
    if (settingRef) return;
    setSettingRef(true);
    try {
      const res = await fetch(`/api/projects/${projectId}/reference`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_id: imageId }),
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.error?.message ?? "Failed to set reference");
      }
      setReferenceId(imageId);
      setImages((prev) => prev.map((img) => ({ ...img, is_reference: img.id === imageId })));
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Failed to set reference image");
    } finally {
      setSettingRef(false);
    }
  }

  const uploadedImages = images.filter((i) => i.status === "uploaded" || i.status === "done");
  const canProceed = referenceId !== null && uploadedImages.length >= 1;

  return (
    <Shell
      crumbs={[
        { label: "Project", href: `/projects/${projectId}` },
        { label: "Upload" },
      ]}
      rightSlot={
        <Button
          size="md"
          disabled={!canProceed}
          onClick={() => router.push(`/projects/${projectId}/annotate`)}
        >
          Next: Annotate <ArrowRight size={14} />
        </Button>
      }
    >
      <PageTitle
        title="Upload images"
        description="Add 10–50 similar lab images. Then click one to mark it as the reference for annotation."
      />

      <div
        {...getRootProps()}
        className={[
          "relative rounded-2xl border-2 border-dashed p-10 text-center cursor-pointer transition-all overflow-hidden",
          isDragActive
            ? "border-[var(--primary)] bg-[var(--primary-soft)]"
            : "border-[var(--border-strong)] bg-[var(--surface)] hover:border-[var(--primary)] hover:bg-[var(--primary-soft)]/50",
        ].join(" ")}
      >
        <input {...getInputProps()} />
        <div className="grid place-items-center w-14 h-14 mx-auto rounded-2xl bg-gradient-to-br from-indigo-500/15 to-violet-500/15 text-indigo-600 dark:text-indigo-300 mb-4">
          <Upload size={24} strokeWidth={2} />
        </div>
        <p className="text-[15px] font-medium text-[var(--foreground)]">
          {isDragActive ? "Drop your images here" : "Drag & drop images, or click to browse"}
        </p>
        <p className="text-xs text-[var(--muted)] mt-1">
          JPG or PNG · max 20 MB each · up to 50 images
        </p>
      </div>

      {images.length > 0 && (
        <section className="mt-8">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-[var(--foreground)]">Your images</h3>
              <Badge tone="neutral">{images.length}</Badge>
            </div>
            <p className="text-xs text-[var(--muted)]">
              Click an image to set it as the{" "}
              <span className="font-medium text-[var(--foreground)]">reference</span>
            </p>
          </div>

          <div className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-6 gap-3">
            {images.map((img) => {
              const isUploaded = img.status === "uploaded";
              return (
                <button
                  key={img.id}
                  type="button"
                  onClick={() => isUploaded && handleSetReference(img.id)}
                  disabled={!isUploaded || settingRef}
                  className={[
                    "group relative rounded-xl overflow-hidden border-2 transition-all bg-[var(--surface-muted)]",
                    isUploaded
                      ? img.is_reference
                        ? "border-[var(--primary)] ring-2 ring-[var(--primary)]/30 cursor-default"
                        : "border-transparent cursor-pointer hover:border-[var(--primary)]/60 hover:shadow-md hover:shadow-indigo-500/10 hover:-translate-y-0.5"
                      : "border-transparent cursor-default",
                  ].join(" ")}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={img.localUrl ?? img.signed_url ?? ""}
                    alt={img.filename}
                    className="w-full aspect-square object-cover"
                  />

                  {img.uploading && (
                    <div className="absolute inset-0 bg-black/45 grid place-items-center">
                      <div className="w-8 h-8 border-2 border-white border-t-transparent rounded-full animate-spin" />
                    </div>
                  )}

                  {img.status === "failed" && (
                    <div className="absolute inset-0 bg-red-900/60 grid place-items-center">
                      <X size={20} className="text-white" />
                    </div>
                  )}

                  {img.is_reference && (
                    <div className="absolute top-1.5 left-1.5 bg-gradient-to-br from-indigo-500 to-violet-600 text-white text-[10px] font-semibold px-1.5 py-0.5 rounded-md flex items-center gap-1 shadow-sm">
                      <Star size={9} strokeWidth={3} fill="currentColor" /> REF
                    </div>
                  )}

                  {isUploaded && !img.is_reference && (
                    <div className="absolute bottom-1.5 right-1.5">
                      <CheckCircle size={14} className="text-emerald-400 drop-shadow" />
                    </div>
                  )}

                  {!isUploaded && !img.uploading && img.status !== "failed" && (
                    <div className="absolute inset-0 bg-[var(--surface-muted)] grid place-items-center text-[var(--subtle)]">
                      <ImageIcon size={20} />
                    </div>
                  )}
                </button>
              );
            })}
          </div>
        </section>
      )}

      {!canProceed && images.length > 0 && (
        <p className="mt-6 text-xs text-[var(--muted)]">
          {referenceId
            ? "Wait for uploads to finish, then continue to annotate."
            : "Pick a reference image to continue."}
        </p>
      )}
    </Shell>
  );
}
