import Dexie, { type EntityTable } from "dexie";
import type { Segment, Toc } from "@/lib/api/types";

/** Écriture /api/me en attente (hors ligne) ; `key` dédoublonne : la plus récente gagne. */
export type OutboxEntry = {
  key: string;
  method: "PUT" | "PATCH" | "DELETE" | "POST";
  url: string;
  body?: unknown;
  createdAt: number;
  attempts: number;
};

/** Livre téléchargé : métadonnées, table des matières et tous les segments. */
export type OfflineBook = {
  key: string; // `${editionId}:${revision}`
  editionId: string;
  revision: number;
  workId: string;
  title: string;
  authors: string;
  language: string;
  toc: Toc;
  totalSegments: number;
  bytes: number;
  downloadedAt: number;
};

export type OfflineSegments = {
  key: string; // `${editionId}:${revision}:${fromSeq}`
  book: string; // OfflineBook.key
  fromSeq: number;
  segments: Segment[];
  notes: Record<string, unknown> | null;
};

/** Progression locale d'une œuvre (écrite à chaque changement, envoyée ensuite). */
export type LocalProgress = {
  workId: string;
  body: ProgressBody;
};

export type ProgressBody = {
  editionId: string;
  revision: number;
  seq: number;
  offset: number;
  quote: { exact: string; prefix: string; suffix: string };
  progress: number;
  sectionPath: string[];
  startedAt: string;
  updatedAt: string;
  finishedAt: string | null;
};

class ReaderDB extends Dexie {
  outbox!: EntityTable<OutboxEntry, "key">;
  books!: EntityTable<OfflineBook, "key">;
  segments!: EntityTable<OfflineSegments, "key">;
  workProgress!: EntityTable<LocalProgress, "workId">;
  kv!: EntityTable<{ key: string; value: unknown }, "key">;

  constructor() {
    super("swann-reader");
    this.version(1).stores({
      outbox: "key, createdAt",
      books: "key, editionId, downloadedAt",
      segments: "key, book, fromSeq",
      progress: "editionId",
      kv: "key",
    });
    // Progression par œuvre (et non plus par édition) : Dexie ne change pas de clé, nouvelle table
    this.version(2).stores({ progress: null, workProgress: "workId" });
  }
}

let instance: ReaderDB | null = null;

/** Base IndexedDB du navigateur (null côté serveur). */
export function localDb(): ReaderDB | null {
  if (typeof indexedDB === "undefined") return null;
  instance ??= new ReaderDB();
  return instance;
}
