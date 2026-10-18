"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, RefreshCcw } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Confirm, ErrorBox, Loading } from "@/components/common/misc";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useStartJob } from "@/lib/actions";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { dateTime, num } from "@/lib/format";

export function IndexesPage() {
  const qc = useQueryClient();
  const start = useStartJob();
  const [all, setAll] = useState(false);
  const indexes = useQuery({
    queryKey: ["indexes"],
    queryFn: async () => unwrap(await api.GET("/v1/admin/indexes")),
  });
  const consistency = useQuery({
    queryKey: ["consistency", all],
    queryFn: async () =>
      unwrap(
        await api.GET("/v1/admin/consistency", {
          params: { query: { only_problems: !all, limit: 2000 } },
        }),
      ),
    enabled: false,
  });
  const activate = useMutation({
    mutationFn: async (collection: string) =>
      unwrap(
        await api.POST("/v1/admin/indexes/{collection}/activate", {
          params: { path: { collection } },
        }),
      ),
    onSuccess: (_, c) => {
      toast.success(`La recherche utilise maintenant ${c}`);
      void qc.invalidateQueries({ queryKey: ["indexes"] });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  return (
    <>
      <PageHeader
        title="Index et cohérence"
        description="Index vectoriels (une collection Qdrant par modèle et découpage) et vérification Postgres ↔ Qdrant ↔ MinIO."
      />
      <Card>
        <CardHeader>
          <CardTitle>Index vectoriels</CardTitle>
          <span className="text-xs text-muted-foreground">
            création : swann index create … puis remplissage par le worker
          </span>
        </CardHeader>
        <CardContent>
          {indexes.isLoading ? (
            <Loading rows={2} />
          ) : indexes.error ? (
            <ErrorBox error={indexes.error} />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Collection</TableHead>
                  <TableHead>Modèle</TableHead>
                  <TableHead>Statut</TableHead>
                  <TableHead className="text-right">Éditions</TableHead>
                  <TableHead className="text-right">Points</TableHead>
                  <TableHead className="text-right">À indexer</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {indexes.data?.map((i) => (
                  <TableRow key={i.id}>
                    <TableCell className="font-mono text-xs">{i.collection}</TableCell>
                    <TableCell className="text-xs">
                      {i.dense_model}{" "}
                      <span className="text-muted-foreground">
                        ({i.dense_dim} dim., {i.chunker_version})
                      </span>
                      <div className="text-muted-foreground">créé {dateTime(i.created_at)}</div>
                    </TableCell>
                    <TableCell>
                      {i.status === "active" ? (
                        <span className="inline-flex items-center gap-1 text-xs font-medium text-ok">
                          <CheckCircle2 className="size-3.5" aria-hidden /> actif
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">{i.status}</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular">{num(i.editions)}</TableCell>
                    <TableCell className="text-right tabular">{num(i.points)}</TableCell>
                    <TableCell className="text-right tabular">{num(i.pending)}</TableCell>
                    <TableCell className="text-right">
                      {i.status !== "active" && (
                        <Confirm
                          trigger={
                            <Button variant="outline" size="sm">
                              Activer
                            </Button>
                          }
                          title={`Activer ${i.collection} ?`}
                          description={
                            i.pending > 0
                              ? `${i.pending} édition(s) ne sont pas encore indexées : elles seront absentes de la recherche.`
                              : "La recherche des applications bascule immédiatement sur cet index."
                          }
                          confirmLabel="Activer"
                          onConfirm={() => activate.mutate(i.collection)}
                        />
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card className="mt-4">
        <CardHeader>
          <CardTitle>Cohérence des stockages</CardTitle>
          <div className="flex items-center gap-3">
            <Label className="flex items-center gap-2 text-xs">
              <Checkbox checked={all} onCheckedChange={(v) => setAll(v === true)} /> tout afficher
            </Label>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void consistency.refetch()}
              disabled={consistency.isFetching}
            >
              {consistency.isFetching ? (
                <Loader2 className="animate-spin" aria-hidden />
              ) : (
                <RefreshCcw aria-hidden />
              )}
              Vérifier
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {!consistency.data && !consistency.isFetching && (
            <p className="text-sm text-muted-foreground">
              Compare, pour chaque édition, les chunks en base, les points déclarés, les points
              réellement dans Qdrant et la présence de l'EPUB dans MinIO.
            </p>
          )}
          {consistency.error && <ErrorBox error={consistency.error} />}
          {consistency.data && (
            <>
              <p className="mb-3 text-sm">
                {num(consistency.data.checked)} édition(s) vérifiée(s),{" "}
                <strong className={consistency.data.with_problems ? "text-bad" : "text-ok"}>
                  {num(consistency.data.with_problems)} avec un écart
                </strong>{" "}
                (index {consistency.data.active_collection ?? "aucun"}).
              </p>
              {consistency.data.rows.length > 0 && (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Édition</TableHead>
                      <TableHead className="text-right">Chunks</TableHead>
                      <TableHead className="text-right">Déclarés</TableHead>
                      <TableHead className="text-right">Qdrant</TableHead>
                      <TableHead>Écarts</TableHead>
                      <TableHead />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {consistency.data.rows.map((r) => (
                      <TableRow key={r.edition_id}>
                        <TableCell className="max-w-xs">
                          <div className="truncate font-medium">{r.title}</div>
                          <div className="truncate text-xs text-muted-foreground">
                            {r.source_file}
                          </div>
                        </TableCell>
                        <TableCell className="text-right tabular">{num(r.chunks)}</TableCell>
                        <TableCell className="text-right tabular">
                          {num(r.indexed_points)}
                        </TableCell>
                        <TableCell className="text-right tabular">{num(r.qdrant_points)}</TableCell>
                        <TableCell className="text-xs text-bad">
                          {r.problems.join(" ; ") || <span className="text-ok">ok</span>}
                        </TableCell>
                        <TableCell className="text-right">
                          {r.problems.some((p) => !p.startsWith("EPUB")) && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() =>
                                start.mutate({
                                  kind: "reprocess",
                                  params: { edition_id: r.edition_id },
                                  title: `Réindexation de ${r.title}`,
                                })
                              }
                            >
                              Réindexer
                            </Button>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </>
  );
}
