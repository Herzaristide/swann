import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** Libellés des signaux de qualité (codes stables de swann_ingest.quality). */
export const SIGNALS: Record<string, { label: string; severity: "high" | "medium" | "low" }> = {
  empty_body: { label: "Corps vide", severity: "high" },
  language_mismatch: { label: "Langue incohérente", severity: "high" },
  not_indexed: { label: "Pas indexée", severity: "high" },
  giant_segments: { label: "Paragraphes collés", severity: "medium" },
  tiny_segments: { label: "Segments très courts", severity: "medium" },
  alignment_doubtful: { label: "Alignement douteux", severity: "medium" },
  no_toc: { label: "Pas de table des matières", severity: "low" },
  thin_body: { label: "Corps très court", severity: "low" },
  out_of_body: { label: "Beaucoup hors corps", severity: "low" },
  no_chapters: { label: "Structure plate", severity: "low" },
  not_aligned: { label: "Pas alignée", severity: "low" },
  empty_sections: { label: "Sections vides", severity: "low" },
  unreferenced_notes: { label: "Notes non appelées", severity: "low" },
  parse_warnings: { label: "Avertissements de lecture", severity: "low" },
  no_epub: { label: "EPUB absent", severity: "low" },
};

const TONE = {
  high: "border-bad/40 bg-bad/10 text-bad",
  medium: "border-warn/40 bg-warn/10 text-warn",
  low: "border-border bg-muted text-muted-foreground",
};

export function SignalChip({
  code,
  detail,
  acked = false,
}: {
  code: string;
  detail?: string;
  acked?: boolean;
}) {
  const s = SIGNALS[code] ?? { label: code, severity: "low" as const };
  const chip = (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-xs whitespace-nowrap",
        TONE[s.severity],
        acked && "line-through opacity-60",
      )}
    >
      {s.label}
    </span>
  );
  if (!detail) return chip;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{chip}</TooltipTrigger>
      <TooltipContent className="max-w-xs">
        {detail}
        {acked && " (accepté)"}
      </TooltipContent>
    </Tooltip>
  );
}
