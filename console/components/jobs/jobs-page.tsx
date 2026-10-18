"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Empty, ErrorBox, Loading, LoadMore } from "@/components/common/misc";
import { PageHeader } from "@/components/common/page-header";
import { JOB_KIND } from "@/components/common/status";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, unwrap } from "@/lib/api/client";
import { JobTable } from "./job-table";

const STATUS_TABS: { value: string; label: string; statuses?: string[] }[] = [
  { value: "all", label: "Toutes" },
  { value: "active", label: "Actives", statuses: ["queued", "running"] },
  { value: "needs_review", label: "À valider", statuses: ["needs_review"] },
  { value: "failed", label: "Échecs", statuses: ["failed"] },
  { value: "done", label: "Terminées", statuses: ["succeeded", "cancelled"] },
];

export function JobsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const tab = params.get("status") ?? "all";
  const kind = params.get("kind") ?? "all";
  const statuses = STATUS_TABS.find((t) => t.value === tab)?.statuses;

  const q = useInfiniteQuery({
    queryKey: ["jobs", "list", tab, kind],
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/v1/admin/jobs", {
          params: {
            query: {
              limit: 50,
              cursor: pageParam ?? undefined,
              status: statuses,
              kind: kind === "all" ? undefined : [kind],
            },
          },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? null,
  });

  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value === "all") next.delete(key);
    else next.set(key, value);
    router.replace(`${pathname}?${next}` as never);
  };
  const jobs = q.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <>
      <PageHeader
        title="Tâches"
        description="Dépôts, retraitements, alignements, commandes swann : en direct."
      />
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Tabs value={tab} onValueChange={(v) => set("status", v)}>
          <TabsList>
            {STATUS_TABS.map((t) => (
              <TabsTrigger key={t.value} value={t.value}>
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <Select value={kind} onValueChange={(v) => set("kind", v)}>
          <SelectTrigger className="w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Tous les types</SelectItem>
            {Object.entries(JOB_KIND).map(([k, label]) => (
              <SelectItem key={k} value={k}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : jobs.length ? (
        <JobTable jobs={jobs} />
      ) : (
        <Empty>Aucune tâche.</Empty>
      )}
      <LoadMore
        hasNextPage={!!q.hasNextPage}
        isFetching={q.isFetchingNextPage}
        onClick={() => void q.fetchNextPage()}
      />
    </>
  );
}
