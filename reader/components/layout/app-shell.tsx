"use client";

import { Settings } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { CommandPalette } from "@/components/command-palette";
import { UserMenu } from "@/components/layout/user-menu";
import { Logo } from "@/components/logo";
import { cn } from "@/lib/utils";
import { useViewer } from "@/lib/viewer";
import { isActive, NAV_ITEMS } from "./nav-items";

/**
 * Coque de l'application : barre latérale sur grand écran, barre d'onglets
 * en bas sur téléphone, en-tête avec la palette de commande (⌘K).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const viewer = useViewer();

  return (
    <div className="flex min-h-dvh">
      <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 flex-col border-r px-3 py-5 lg:flex">
        <Link href="/" className="mb-8 px-3" aria-label="Swann — accueil">
          <Logo />
        </Link>
        <nav aria-label="Navigation principale" className="flex flex-col gap-0.5">
          {NAV_ITEMS.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              aria-current={isActive(pathname, href) ? "page" : undefined}
              className={cn(
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                "aria-[current=page]:bg-accent aria-[current=page]:font-medium aria-[current=page]:text-foreground",
              )}
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </Link>
          ))}
        </nav>
        <div className="mt-auto flex flex-col gap-0.5">
          {viewer && (
            <Link
              href="/settings"
              aria-current={isActive(pathname, "/settings") ? "page" : undefined}
              className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground aria-[current=page]:bg-accent aria-[current=page]:text-foreground"
            >
              <Settings className="size-4" aria-hidden />
              Réglages
            </Link>
          )}
          <UserMenu />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b bg-background/85 px-4 backdrop-blur-md supports-[backdrop-filter]:bg-background/70 lg:px-8">
          <Link href="/" className="lg:hidden" aria-label="Swann — accueil">
            <Logo compact />
          </Link>
          <div className="flex flex-1 justify-end lg:justify-start">
            <CommandPalette />
          </div>
          <div className="lg:hidden">
            <UserMenu compact />
          </div>
        </header>
        <main className="flex-1 px-4 pt-6 pb-28 lg:px-8 lg:pb-12">{children}</main>
      </div>

      <nav
        aria-label="Navigation principale"
        className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-4 border-t bg-background/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-md lg:hidden"
      >
        {NAV_ITEMS.map(({ href, label, icon: Icon }) => (
          <Link
            key={href}
            href={href}
            aria-current={isActive(pathname, href) ? "page" : undefined}
            className="flex flex-col items-center gap-1 py-2.5 text-[11px] text-muted-foreground aria-[current=page]:text-primary"
          >
            <Icon className="size-5" aria-hidden />
            {label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
