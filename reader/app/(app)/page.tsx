import { BookOpen } from "lucide-react";
import Link from "next/link";
import { ProgressCard } from "@/components/progress-card";
import { EmptyState, Section } from "@/components/section";
import { WorkCard, WorkRow } from "@/components/work-card";
import { corpus } from "@/lib/api/server";
import { getUserSub } from "@/lib/auth";
import { listCollections, listFavorites, recentProgress } from "@/lib/reader-data";

export default async function HomePage() {
  const sub = await getUserSub();
  const api = await corpus();

  const [personal, movements, recent] = await Promise.all([
    sub
      ? Promise.all([
          recentProgress(sub, 6, { unfinished: true }),
          listFavorites(sub),
          listCollections(sub),
        ])
      : null,
    api.GET("/v1/movements", { params: { query: { lang: "fr" } } }),
    api.GET("/v1/works", { params: { query: { lang: "fr", sort: "-year", limit: 12 } } }),
  ]);

  // Découvertes : les courants qui ont le plus d'œuvres, quelques œuvres chacun
  const topMovements = (movements.data ?? [])
    .filter((m) => m.n_works > 0)
    .sort((a, b) => b.n_works - a.n_works)
    .slice(0, 4);
  const byMovement = await Promise.all(
    topMovements.map(async (m) => ({
      movement: m,
      works:
        (
          await api.GET("/v1/works", {
            params: { query: { lang: "fr", movement_id: m.id, limit: 12, sort: "year" } },
          })
        ).data?.items ?? [],
    })),
  );

  const [progress, favorites, collections] = personal ?? [[], [], []];
  const nothing = (recent.data?.items.length ?? 0) === 0;

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-10">
        <h1 className="font-serif text-3xl font-semibold tracking-tight sm:text-4xl">
          {sub ? "Bonjour" : "Bienvenue sur Swann"}
        </h1>
        <p className="mt-2 max-w-xl text-muted-foreground">
          Les grandes œuvres dans leur langue et en traduction, côte à côte.
        </p>
      </div>

      {progress.length > 0 && (
        <Section title="Continuer la lecture" href="/library">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {progress.map((p) => (
              <ProgressCard key={p.editionId} p={p} />
            ))}
          </div>
        </Section>
      )}

      {favorites.length > 0 && (
        <Section title="Favoris" href="/library?tab=favorites">
          <WorkRow>
            {favorites.map((f) => f.work && <WorkCard key={f.workId} work={f.work} />)}
          </WorkRow>
        </Section>
      )}

      {collections.length > 0 && (
        <Section title="Collections" href="/library?tab=collections">
          <div className="flex flex-wrap gap-2">
            {collections.map((c) => (
              <Link
                key={c.id}
                href={`/library/collections/${c.id}`}
                className="rounded-full border px-4 py-1.5 text-sm hover:bg-accent"
              >
                {c.emoji && <span className="mr-1.5">{c.emoji}</span>}
                {c.name}
                <span className="ml-2 text-muted-foreground tabular-nums">{c.count}</span>
              </Link>
            ))}
          </div>
        </Section>
      )}

      {nothing ? (
        <EmptyState icon={<BookOpen />} title="Aucune œuvre n'est encore publiée">
          {sub
            ? "Le corpus ne contient pas encore d'édition que votre compte peut lire."
            : "Connectez-vous pour accéder au corpus, ou revenez quand des éditions du domaine public seront ouvertes."}
        </EmptyState>
      ) : (
        <>
          <Section title="Récemment ajoutées au catalogue" href="/explore">
            <WorkRow>
              {recent.data?.items.map((w) => (
                <WorkCard key={w.id} work={w} />
              ))}
            </WorkRow>
          </Section>
          {byMovement.map(
            ({ movement, works }) =>
              works.length > 0 && (
                <Section
                  key={movement.id}
                  title={movement.label}
                  href={`/movements/${movement.id}`}
                >
                  <WorkRow>
                    {works.map((w) => (
                      <WorkCard key={w.id} work={w} />
                    ))}
                  </WorkRow>
                </Section>
              ),
          )}
        </>
      )}
    </div>
  );
}
