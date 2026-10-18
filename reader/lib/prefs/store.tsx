"use client";

import { useTheme } from "next-themes";
import {
  createContext,
  type ReactNode,
  use,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { send } from "@/lib/offline/outbox";
import { DEFAULT_PREFERENCES, type Preferences, parsePreferences } from "./schema";

/**
 * Préférences du lecteur : appliquées immédiatement, gardées dans
 * localStorage (visiteur ou hors ligne) et, si le lecteur est connecté,
 * envoyées à /api/me/preferences. Entre appareils, le plus récent gagne.
 */

type Stored = { data: Preferences; updatedAt: string };

type Ctx = {
  prefs: Preferences;
  setPrefs: (patch: Partial<Preferences>) => void;
  reset: () => void;
};

const PrefsContext = createContext<Ctx | null>(null);
const STORAGE_KEY = "swann:prefs";
const EPOCH = new Date(0).toISOString();

function readLocal(): Stored | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Stored>;
    return { data: parsePreferences(parsed.data), updatedAt: parsed.updatedAt ?? EPOCH };
  } catch {
    return null;
  }
}

function writeLocal(s: Stored) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    // stockage plein ou interdit (navigation privée) : préférences de session
  }
}

export function PreferencesProvider({
  children,
  signedIn,
  initial,
}: {
  children: ReactNode;
  signedIn: boolean;
  initial: Stored | null;
}) {
  const [stored, setStored] = useState<Stored>(
    () => initial ?? { data: DEFAULT_PREFERENCES, updatedAt: EPOCH },
  );
  const current = useRef(stored);
  const { setTheme } = useTheme();
  const pushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Au montage : la version locale l'emporte si elle est plus récente
  useEffect(() => {
    const local = readLocal();
    if (!local || local.updatedAt <= current.current.updatedAt) return;
    current.current = local;
    setStored(local);
    if (signedIn) void send("PUT", "/api/me/preferences", local, { key: "preferences" });
  }, [signedIn]);

  useEffect(() => {
    current.current = stored;
    setTheme(stored.data.theme);
    writeLocal(stored);
  }, [stored, setTheme]);

  const commit = useCallback(
    (data: Preferences) => {
      const next = { data, updatedAt: new Date().toISOString() };
      current.current = next;
      setStored(next);
      if (!signedIn) return;
      if (pushTimer.current) clearTimeout(pushTimer.current);
      // Regroupe les réglages faits à la suite (curseur de taille…)
      pushTimer.current = setTimeout(() => {
        void send("PUT", "/api/me/preferences", next, { key: "preferences" });
      }, 800);
    },
    [signedIn],
  );

  const setPrefs = useCallback(
    (patch: Partial<Preferences>) =>
      commit(parsePreferences({ ...current.current.data, ...patch })),
    [commit],
  );

  const reset = useCallback(() => commit(DEFAULT_PREFERENCES), [commit]);

  return <PrefsContext value={{ prefs: stored.data, setPrefs, reset }}>{children}</PrefsContext>;
}

export function usePreferences(): Ctx {
  const ctx = use(PrefsContext);
  if (!ctx) throw new Error("usePreferences hors de PreferencesProvider");
  return ctx;
}
