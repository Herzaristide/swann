import type { components } from "./schema";

type S = components["schemas"];

export type Overview = S["Overview"];
export type JobSummary = S["JobSummary"];
export type JobDetail = S["JobDetail"];
export type JobStatus = JobSummary["status"];
export type JobKind = JobSummary["kind"];
export type QualityRow = S["QualityRow"];
export type SignalInfo = S["SignalInfo"];
export type Consistency = S["Consistency"];
export type IndexInfo = S["AdminIndex"];
export type CorpusEvent = S["Event"];
export type UploadResult = S["UploadResult"];
export type Resolution = S["Resolution"];
export type AdminWork = S["AdminWork"];
export type AdminWorkRow = S["AdminWorkRow"];
export type AdminEditionRow = S["AdminEditionRow"];
export type WorkPatch = S["WorkPatch"];
export type EditionPatch = S["EditionPatch"];
export type AdminPerson = S["AdminPerson"];
export type AdminPersonRow = S["AdminPersonRow"];
export type AdminMovement = S["AdminMovement"];
export type Structure = S["Structure"];
export type StructureSection = S["StructureSection"];
export type SectionPatch = S["SectionPatch"];
export type SegmentPreview = S["SegmentPreview"];
export type TrashItem = S["TrashItem"];
export type AlignedEdition = S["AlignedEdition"];
export type AlignmentMap = S["AlignmentMap"];
export type Workbench = S["Workbench"];
export type WorkbenchTarget = S["WorkbenchTarget"];
export type Precision = S["Precision"];
export type AlignmentLink = S["AlignmentLink"];

/** Rapport de classement d'un dépôt (swann_ingest.identify.Report, en JSON). */
export type Identification = {
  metadata: {
    titles: string[];
    languages: string[];
    creators: { name: string; role: string | null }[];
    publisher: string | null;
    date: string | null;
  };
  language: string | null;
  detected_language: string | null;
  authors: {
    name: string;
    person_id: string | null;
    display_name: string | null;
    score: number;
    wikidata_id: string | null;
    wikidata_label: string | null;
  }[];
  translators: string[];
  title: string | null;
  candidates: {
    work_id: string;
    title: string;
    slug: string | null;
    original_language: string | null;
    authors: string[];
    title_score: number;
    author_score: number;
    content_score: number | null;
    wikidata_match: boolean;
    score: number;
    editions: {
      id: string;
      language: string;
      title: string;
      is_original: boolean;
      translators: string[];
    }[];
    same_language?: string[];
    duplicates: string[];
    reasons: string[];
  }[];
  wikidata: {
    id: string;
    labels: string[];
    year: number | null;
    original_language: string | null;
    authors: string[];
  } | null;
  wikidata_error: string | null;
  warnings: string[];
  decision: "attach" | "create_work" | "review";
  confident: boolean;
  proposal: Resolution | null;
};
