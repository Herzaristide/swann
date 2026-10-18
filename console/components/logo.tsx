/** Ibis stylisé (Swann) et nom de l'application. */
export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2 font-serif text-lg font-semibold tracking-tight">
      <svg viewBox="0 0 32 32" className="size-7 text-primary" aria-hidden>
        <rect width="32" height="32" rx="8" fill="currentColor" />
        <path
          d="M9 23c4-1 6-4 6-8 0-3 2-5 5-5 2 0 3 1 3 2-2 0-3 1-3 3 0 5-4 9-11 8Z"
          fill="var(--primary-foreground)"
        />
        <path
          d="M23 12c2 1 3 3 3 6"
          stroke="var(--primary-foreground)"
          strokeWidth="1.6"
          fill="none"
          strokeLinecap="round"
        />
      </svg>
      {!compact && (
        <span>
          Swann <span className="font-sans text-sm font-medium text-muted-foreground">console</span>
        </span>
      )}
    </span>
  );
}
