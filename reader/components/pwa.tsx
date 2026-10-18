"use client";

import { useSerwist } from "@serwist/turbopack/react";
import { useEffect } from "react";
import { toast } from "sonner";

type BeforeInstallPromptEvent = Event & { prompt: () => Promise<void> };

const VISITS_KEY = "swann:visits";
const DISMISSED_KEY = "swann:install-dismissed";

/**
 * Bandeau « nouvelle version » (mise à jour à la demande, jamais en pleine
 * lecture) et invitation discrète à installer l'application après 2 visites.
 */
export function Pwa() {
  const { serwist } = useSerwist();

  useEffect(() => {
    if (!serwist) return;
    const onWaiting = () => {
      toast("Nouvelle version disponible", {
        duration: Number.POSITIVE_INFINITY,
        action: {
          label: "Recharger",
          onClick: () => {
            serwist.addEventListener("controlling", () => window.location.reload());
            serwist.messageSkipWaiting();
          },
        },
      });
    };
    serwist.addEventListener("waiting", onWaiting);
    return () => serwist.removeEventListener("waiting", onWaiting);
  }, [serwist]);

  useEffect(() => {
    let visits = 0;
    try {
      visits = Number(localStorage.getItem(VISITS_KEY) ?? 0) + 1;
      localStorage.setItem(VISITS_KEY, String(visits));
      if (localStorage.getItem(DISMISSED_KEY)) return;
    } catch {
      return;
    }
    if (visits < 2 || window.matchMedia("(display-mode: standalone)").matches) return;

    const dismiss = () => {
      try {
        localStorage.setItem(DISMISSED_KEY, "1");
      } catch {}
    };

    const onPrompt = (e: Event) => {
      e.preventDefault();
      const evt = e as BeforeInstallPromptEvent;
      toast("Installer Swann sur cet appareil ?", {
        description: "Lecture plein écran, et hors ligne pour les livres téléchargés.",
        duration: 15000,
        action: { label: "Installer", onClick: () => void evt.prompt().finally(dismiss) },
        onDismiss: dismiss,
      });
    };
    window.addEventListener("beforeinstallprompt", onPrompt);

    // iOS : pas d'événement d'installation, on explique le geste
    const ios =
      /iphone|ipad|ipod/i.test(navigator.userAgent) &&
      !("standalone" in navigator && navigator.standalone);
    if (ios) {
      toast("Installer Swann", {
        description: "Touchez Partager, puis « Sur l'écran d'accueil ».",
        duration: 12000,
        onDismiss: dismiss,
        onAutoClose: dismiss,
      });
    }
    return () => window.removeEventListener("beforeinstallprompt", onPrompt);
  }, []);

  return null;
}
