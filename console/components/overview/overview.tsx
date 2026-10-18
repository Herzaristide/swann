"use client";

import { useQuery } from "@tanstack/react-query";
import { Cpu, Database, HardDrive, Play, RefreshCcw, Server, Trash2 } from "lucide-react";
import Link from "next/link";
import { ErrorBox, Loading, Stat } from "@/components/common/misc";
import { PageHeader } from "@/components/common/page-header";
import { SIGNALS } from "@/components/common/signals";
import { JobTable } from "@/components/jobs/job-table";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useStartJob } from "@/lib/actions";
import { api, unwrap } from "@/lib/api/client";
import type { Overview } from "@/lib/api/types";
import { bytes, languageName, num, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

function Health({ status, detail }: { status: string; detail?: string | null }) {
  const tone = status === "ok" ? "bg-ok" : status === "disabled" ? "bg-muted-foreground" : "bg-bad";
  const label =
    status === "ok" ? "opérationnel" : status === "disabled" ? "désactivé" : "en erreur";
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
      title={detail ?? undefined}
    >
      <span className={cn("size-2 rounded-full", tone)} aria-hidden />
      {label}
    </span>
  );
}

function ServiceCard({
  icon: Icon,
  title,
  status,
  detail,
  children,
}: {
  icon: typeof Database;
  title: string;
  status: string;
  detail?: string | null;
  children: React.ReactNode;
}) {
  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardTitle className="flex items-center gap-2">
          <Icon className="size-4 text-muted-foreground" aria-hidden /> {title}
        </CardTitle>
        <Health status={status} detail={detail} />
      </CardHeader>
      <CardContent className="px-4 text-sm">
        {status === "error" && detail ? <p className="text-xs text-bad">{detail}</p> : children}
      </CardContent>
    </Card>
  );
}

function Bar({ value, max, className }: { value: number; max: number; className?: string }) {
  const pct = max ? Math.max(value ? 1.5 : 0, (value / max) * 100) : 0;
  return (
    <div className="h-2.5 w-full rounded-full bg-muted">
      <div
        className={cn("h-full rounded-full bg-primary", className)}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function Funnel({ data }: { data: Overview }) {
  const f = data.funnel;
  const steps = [
    { label: "Éditions", value: f.editions, className: "bg-muted-foreground/60" },
    { label: "Extraites", value: f.extracted, className: "bg-info" },
    { label: "Indexées (index actif)", value: f.indexed, className: "bg-primary" },
    { label: "Alignées (fiables)", value: f.aligned_reliable, className: "bg-ok" },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Avancement du corpus</CardTitle>
        <span className="text-xs text-muted-foreground">
          {num(f.works)} œuvres · {num(f.trashed)} à la corbeille
        </span>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {steps.map((s) => (
          <div key={s.label} className="grid grid-cols-[10rem_1fr_4rem] items-center gap-3 text-sm">
            <span className="text-muted-foreground">{s.label}</span>
            <Bar value={s.value} max={f.editions} className={s.className} />
            <span className="text-right tabular">{num(s.value)}</span>
          </div>
        ))}
        {f.aligned_other > 0 && (
          <p className="text-xs text-muted-foreground">
            + {num(f.aligned_other)} édition(s) alignée(s) mais douteuse(s) ou rejetée(s) —{" "}
            <Link href="/alignment" className="underline">
              atelier d'alignement
            </Link>
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function Languages({ data }: { data: Overview }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Par langue</CardTitle>
      </CardHeader>
      <CardContent className="max-h-80 overflow-y-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Langue</TableHead>
              <TableHead className="text-right">Éditions</TableHead>
              <TableHead className="text-right">Indexées</TableHead>
              <TableHead className="text-right">Alignées</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.languages.map((l) => (
              <TableRow key={l.language}>
                <TableCell>
                  {languageName(l.language)}{" "}
                  <span className="text-xs text-muted-foreground">{l.language}</span>
                </TableCell>
                <TableCell className="text-right tabular">{num(l.editions)}</TableCell>
                <TableCell className="text-right tabular">{num(l.indexed)}</TableCell>
                <TableCell className="text-right tabular">{num(l.aligned_reliable)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function Quality({ data }: { data: Overview }) {
  const max = Math.max(1, ...data.quality_signals.map((s) => s.count));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Qualité</CardTitle>
        <span className="text-xs text-muted-foreground">
          score moyen{" "}
          <strong className="text-foreground tabular">
            {data.quality_mean_score === null ? "–" : Math.round(data.quality_mean_score)}
          </strong>
          /100
        </span>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {data.quality_signals.length === 0 && (
          <p className="text-sm text-muted-foreground">Aucun signal à traiter.</p>
        )}
        {data.quality_signals.slice(0, 8).map((s) => (
          <Link
            key={s.key}
            href={`/quality?signal=${s.key}`}
            className="grid grid-cols-[11rem_1fr_3rem] items-center gap-3 rounded text-sm hover:bg-accent/60"
          >
            <span className="truncate">{SIGNALS[s.key]?.label ?? s.key}</span>
            <Bar value={s.count} max={max} className="bg-warn" />
            <span className="text-right tabular">{s.count}</span>
          </Link>
        ))}
      </CardContent>
    </Card>
  );
}

export function OverviewPage() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["overview"],
    queryFn: async () => unwrap(await api.GET("/v1/admin/overview")),
    refetchInterval: 30_000,
  });
  const start = useStartJob();

  const actions = (
    <>
      <Button variant="outline" size="sm" onClick={() => start.mutate({ kind: "import_books" })}>
        <Play aria-hidden /> Importer books/
      </Button>
      <Button variant="outline" size="sm" onClick={() => start.mutate({ kind: "quality" })}>
        <RefreshCcw aria-hidden /> Recalculer la qualité
      </Button>
      <Button variant="outline" size="sm" onClick={() => start.mutate({ kind: "purge" })}>
        <Trash2 aria-hidden /> Purger la corbeille
      </Button>
    </>
  );

  if (isLoading) return <Loading rows={8} />;
  if (error || !data) return <ErrorBox error={error} />;
  const pg = data.postgres;
  const alive = data.workers.filter((w) => w.alive);
  const running = data.running.filter((j) => j.status === "running");
  const toReview = data.running.filter((j) => j.status === "needs_review");

  return (
    <>
      <PageHeader
        title="Vue d'ensemble"
        description={`Mise à jour ${relativeTime(data.generated_at)} — en direct pour les tâches.`}
        actions={actions}
      />
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <ServiceCard icon={Database} title="Postgres" status={pg.status} detail={pg.detail}>
          <p className="tabular">
            {bytes(pg.size_bytes ?? 0)} · {pg.connections} connexion(s)
          </p>
          <p className="text-xs text-muted-foreground">
            {pg.migrations_applied} migrations, dernière {pg.last_migration}
          </p>
        </ServiceCard>
        <ServiceCard
          icon={Server}
          title="Qdrant"
          status={data.qdrant.status}
          detail={data.qdrant.detail}
        >
          {data.qdrant.collections
            .filter((c) => c.is_active)
            .map((c) => (
              <p key={c.name} className="tabular">
                {num(c.points)} points · {c.status}
              </p>
            ))}
          <p
            className="truncate text-xs text-muted-foreground"
            title={data.qdrant.alias_target ?? ""}
          >
            alias {data.qdrant.alias} → {data.qdrant.alias_target ?? "aucun"}
          </p>
        </ServiceCard>
        <ServiceCard
          icon={HardDrive}
          title="Stockage"
          status={data.storage.status}
          detail={data.storage.detail}
        >
          {data.storage.buckets.map((b) => (
            <p key={b.name} className="tabular">
              {b.name} :{" "}
              {b.exists
                ? `${num(b.objects)}${b.truncated ? "+" : ""} fichiers, ${bytes(b.size_bytes ?? 0)}`
                : "absent"}
            </p>
          ))}
        </ServiceCard>
        <ServiceCard
          icon={Cpu}
          title="Worker"
          status={alive.length ? "ok" : "error"}
          detail={alive.length ? null : "Aucun worker actif : les tâches attendent (swann worker)."}
        >
          {alive.map((w) => (
            <div key={w.id}>
              <p className="truncate">
                {w.device === "cuda" ? (w.gpu_name ?? "GPU") : "CPU"}
                <span className="text-xs text-muted-foreground"> · {w.hostname}</span>
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {w.current_job_title ? `occupé : ${w.current_job_title}` : "en attente de tâches"}
              </p>
            </div>
          ))}
        </ServiceCard>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <Stat label="Tâches en cours" value={running.length} />
        <Stat
          label="Dépôts à valider"
          value={toReview.length}
          hint={
            toReview.length ? (
              <Link href="/jobs?status=needs_review" className="underline">
                les traiter
              </Link>
            ) : undefined
          }
        />
        <Stat
          label="Échecs (7 jours)"
          value={data.jobs_by_status.find((s) => s.key === "failed")?.count ?? 0}
        />
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <Funnel data={data} />
        <Quality data={data} />
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-[2fr_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Tâches actives</CardTitle>
            <Link href="/jobs" className="text-xs text-muted-foreground underline">
              toutes les tâches
            </Link>
          </CardHeader>
          <CardContent>
            {data.running.length ? (
              <JobTable jobs={data.running} />
            ) : (
              <p className="text-sm text-muted-foreground">Aucune tâche en cours ni en attente.</p>
            )}
            {data.recent_failures.length > 0 && (
              <>
                <h3 className="mt-5 mb-1 text-xs font-medium text-muted-foreground">
                  Derniers échecs
                </h3>
                <JobTable jobs={data.recent_failures} />
              </>
            )}
          </CardContent>
        </Card>
        <Languages data={data} />
      </div>
    </>
  );
}
