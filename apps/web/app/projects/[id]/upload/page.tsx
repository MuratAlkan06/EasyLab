"use client";

import { startTransition, useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useDropzone } from "react-dropzone";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Upload, CheckCircle, X, Star, ArrowRight, ChevronRight, Layers } from "lucide-react";
import { ReactQueryProvider } from "@/lib/query-client";

type ImageItem = {
  id: string;
  filename: string;
  status: string;
  is_reference: boolean;
  signed_url: string | null;
  // local-only (before confirm)
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
    return () => { mountedRef.current = false; };
  }, []);

  // Load existing images on mount
  const { data: existingImages } = useQuery({
    queryKey: ["images", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/images`);
      if (!res.ok) throw new Error("Failed to load images");
      return res.json() as Promise<{ images: ImageItem[] }>;
    },
  });

  useEffect(() => {
    if (existingImages?.images) {
      const uploaded = existingImages.images.filter((i) => i.status !== "pending_upload");
      startTransition(() => {
        setImages(uploaded);
        const ref = uploaded.find((i) => i.is_reference);
        if (ref) setReferenceId(ref.id);
      });
    }
  }, [existingImages]);

  const uploadFiles = useCallback(async (files: File[]) => {
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

    // Request signed upload URLs
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

    // Add placeholder items while uploading
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

    // Upload each file directly to Supabase Storage
    await Promise.all(
      urlData.uploads.map(async (upload, i) => {
        try {
          await fetch(upload.upload_url, {
            method: "PUT",
            headers: { "Content-Type": validFiles[i].type },
            body: validFiles[i],
          });

          // Confirm upload
          await fetch(`/api/projects/${projectId}/images/${upload.image_id}/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              size_bytes: validFiles[i].size,
            }),
          });

          if (!mountedRef.current) return;
          setImages((prev) =>
            prev.map((img) =>
              img.id === upload.image_id
                ? { ...img, status: "uploaded", uploading: false }
                : img
            )
          );
        } catch {
          if (!mountedRef.current) return;
          setImages((prev) =>
            prev.map((img) =>
              img.id === upload.image_id
                ? { ...img, uploading: false, status: "failed" }
                : img
            )
          );
          toast.error(`Failed to upload ${upload.filename}`);
        }
      })
    );

    qc.invalidateQueries({ queryKey: ["images", projectId] });
  }, [projectId, qc]);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop: uploadFiles,
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
      setImages((prev) =>
        prev.map((img) => ({ ...img, is_reference: img.id === imageId }))
      );
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Failed to set reference image");
    } finally {
      setSettingRef(false);
    }
  }

  const uploadedImages = images.filter((i) => i.status === "uploaded" || i.status === "done");
  const canProceed = referenceId !== null && uploadedImages.length >= 1;

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
        <span className="text-sm font-medium text-zinc-900">Upload</span>
      </header>

      <main className="max-w-5xl mx-auto px-8 py-10 space-y-6">
        <div>
          <h2 className="text-xl font-semibold text-zinc-900">Upload Images</h2>
          <p className="text-sm text-zinc-500 mt-1">
            Upload your lab images, then click one to set it as the reference image.
          </p>
        </div>

        {/* Dropzone */}
        <div
          {...getRootProps()}
          className={[
            "border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-colors",
            isDragActive
              ? "border-blue-400 bg-blue-50"
              : "border-zinc-300 bg-white hover:border-zinc-400",
          ].join(" ")}
        >
          <input {...getInputProps()} />
          <Upload size={32} className="mx-auto text-zinc-400 mb-3" />
          <p className="text-sm font-medium text-zinc-700">
            {isDragActive ? "Drop images here" : "Drag & drop images, or click to browse"}
          </p>
          <p className="text-xs text-zinc-400 mt-1">JPG, PNG · Max 20 MB each</p>
        </div>

        {/* Image grid */}
        {images.length > 0 && (
          <div>
            <p className="text-sm text-zinc-500 mb-3">
              Click an image to set it as the{" "}
              <span className="font-medium text-zinc-700">reference image</span> for annotation.
            </p>
            <div className="grid grid-cols-4 gap-3 sm:grid-cols-6">
              {images.map((img) => (
                <div
                  key={img.id}
                  onClick={() => img.status === "uploaded" && handleSetReference(img.id)}
                  className={[
                    "relative rounded-lg overflow-hidden border-2 transition-all",
                    img.status === "uploaded"
                      ? img.is_reference
                        ? "border-blue-500 cursor-default"
                        : "border-transparent cursor-pointer hover:border-zinc-400"
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
                    <div className="absolute inset-0 bg-black/40 flex items-center justify-center">
                      <div className="w-8 h-8 border-2 border-white border-t-transparent rounded-full animate-spin" />
                    </div>
                  )}

                  {img.status === "failed" && (
                    <div className="absolute inset-0 bg-red-900/60 flex items-center justify-center">
                      <X size={20} className="text-white" />
                    </div>
                  )}

                  {img.is_reference && (
                    <div className="absolute top-1 left-1 bg-blue-500 text-white text-xs font-medium px-1.5 py-0.5 rounded flex items-center gap-1">
                      <Star size={10} />
                      Ref
                    </div>
                  )}

                  {img.status === "uploaded" && !img.uploading && (
                    <div className="absolute bottom-1 right-1">
                      <CheckCircle size={16} className="text-green-400" />
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Navigation */}
        <div className="flex justify-end pt-2">
          <button
            disabled={!canProceed}
            onClick={() => router.push(`/projects/${projectId}/annotate`)}
            className="flex items-center gap-2 px-5 py-2.5 bg-zinc-900 text-white text-sm rounded-lg hover:bg-zinc-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            Next: Annotate
            <ArrowRight size={16} />
          </button>
        </div>
      </main>
    </div>
  );
}
