"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronRight, FlaskConical } from "lucide-react";
import type { ReactNode } from "react";

type Crumb = { label: string; href?: string };

export function Shell({
  crumbs = [],
  rightSlot,
  children,
  contentClassName = "",
  fullBleed = false,
}: {
  crumbs?: Crumb[];
  rightSlot?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
  fullBleed?: boolean;
}) {
  return (
    <div className="min-h-screen flex flex-col">
      <Header crumbs={crumbs} rightSlot={rightSlot} />
      {fullBleed ? (
        <main className={`flex-1 min-h-0 ${contentClassName}`}>{children}</main>
      ) : (
        <main className={`flex-1 w-full max-w-6xl mx-auto px-6 sm:px-8 py-10 ${contentClassName}`}>
          {children}
        </main>
      )}
    </div>
  );
}

function Header({ crumbs, rightSlot }: { crumbs: Crumb[]; rightSlot?: ReactNode }) {
  return (
    <header className="sticky top-0 z-30 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--surface)_85%,transparent)] backdrop-blur-md">
      <div className="max-w-6xl mx-auto px-6 sm:px-8 h-14 flex items-center gap-2">
        <Link href="/" className="flex items-center gap-2 group">
          <span className="grid place-items-center w-7 h-7 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-sm shadow-indigo-500/30">
            <FlaskConical size={15} strokeWidth={2.4} />
          </span>
          <span className="font-semibold tracking-tight text-[15px] bg-gradient-to-r from-[var(--foreground)] to-[color-mix(in_oklab,var(--foreground)_70%,var(--primary))] bg-clip-text text-transparent">
            EasyLab
          </span>
        </Link>
        {crumbs.length > 0 && (
          <nav className="flex items-center gap-1.5 text-sm min-w-0">
            {crumbs.map((c, i) => (
              <span key={i} className="flex items-center gap-1.5 min-w-0">
                <ChevronRight size={13} className="text-[var(--subtle)] flex-shrink-0" />
                {c.href ? (
                  <Link
                    href={c.href}
                    className="text-[var(--muted)] hover:text-[var(--foreground)] truncate transition-colors"
                  >
                    {c.label}
                  </Link>
                ) : (
                  <span className="text-[var(--foreground)] font-medium truncate">{c.label}</span>
                )}
              </span>
            ))}
          </nav>
        )}
        <div className="ml-auto flex items-center gap-2">{rightSlot}</div>
      </div>
    </header>
  );
}

// ---------- UI primitives ----------

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
};

const BTN_BASE =
  "inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-all focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed select-none whitespace-nowrap";

const BTN_VARIANT: Record<NonNullable<ButtonProps["variant"]>, string> = {
  primary:
    "bg-gradient-to-b from-indigo-500 to-indigo-600 text-white shadow-sm shadow-indigo-500/30 hover:from-indigo-500 hover:to-indigo-700 active:translate-y-px",
  secondary:
    "bg-[var(--surface)] text-[var(--foreground)] border border-[var(--border-strong)] hover:bg-[var(--surface-muted)] shadow-sm",
  ghost:
    "text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--surface-muted)]",
  danger:
    "bg-red-600 text-white shadow-sm shadow-red-500/30 hover:bg-red-700 active:translate-y-px",
};

const BTN_SIZE: Record<NonNullable<ButtonProps["size"]>, string> = {
  sm: "h-8 px-3 text-xs",
  md: "h-10 px-4 text-sm",
  lg: "h-11 px-5 text-sm",
};

export function Button({
  variant = "primary",
  size = "md",
  loading,
  className = "",
  children,
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={`${BTN_BASE} ${BTN_VARIANT[variant]} ${BTN_SIZE[size]} ${className}`}
    >
      {loading && <Spinner size={size === "sm" ? 12 : 14} />}
      {children}
    </button>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className="animate-spin"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function Card({
  className = "",
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={`bg-[var(--surface)] border border-[var(--border)] rounded-xl shadow-[0_1px_2px_rgba(15,23,42,0.04),0_1px_1px_rgba(15,23,42,0.03)] ${className}`}
    >
      {children}
    </div>
  );
}

export function PageTitle({
  title,
  description,
  rightSlot,
}: {
  title: string;
  description?: string;
  rightSlot?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 mb-6">
      <div>
        <h1 className="text-[22px] sm:text-2xl font-semibold tracking-tight text-[var(--foreground)]">
          {title}
        </h1>
        {description && (
          <p className="text-sm text-[var(--muted)] mt-1.5 max-w-xl">{description}</p>
        )}
      </div>
      {rightSlot && <div className="flex items-center gap-2 flex-shrink-0">{rightSlot}</div>}
    </div>
  );
}

const BADGE_TONE: Record<string, string> = {
  neutral: "bg-[var(--surface-muted)] text-[var(--muted)] border-[var(--border)]",
  blue: "bg-blue-50 text-blue-700 border-blue-100 dark:bg-blue-500/10 dark:text-blue-300 dark:border-blue-400/20",
  green: "bg-emerald-50 text-emerald-700 border-emerald-100 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-400/20",
  amber: "bg-amber-50 text-amber-700 border-amber-100 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-400/20",
  red: "bg-red-50 text-red-700 border-red-100 dark:bg-red-500/10 dark:text-red-300 dark:border-red-400/20",
  indigo: "bg-indigo-50 text-indigo-700 border-indigo-100 dark:bg-indigo-500/10 dark:text-indigo-300 dark:border-indigo-400/20",
};

export function Badge({
  tone = "neutral",
  children,
  className = "",
}: {
  tone?: keyof typeof BADGE_TONE;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border ${BADGE_TONE[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

export function useNav() {
  return useRouter();
}
