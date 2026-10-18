import type { Metadata, Viewport } from "next";
import { Providers } from "@/components/providers";
import { inter, literata, sourceSerif } from "@/lib/fonts";
import { loadViewer } from "@/lib/me";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Swann", template: "%s · Swann" },
  description: "Lire les grandes œuvres, dans leur langue et en traduction.",
  applicationName: "Swann",
  appleWebApp: { capable: true, title: "Swann", statusBarStyle: "default" },
  formatDetection: { telephone: false },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fdfcf9" },
    { media: "(prefers-color-scheme: dark)", color: "#1a1816" },
  ],
  viewportFit: "cover",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const { viewer, prefs } = await loadViewer();
  return (
    <html
      lang="fr"
      suppressHydrationWarning
      className={`${inter.variable} ${literata.variable} ${sourceSerif.variable}`}
    >
      <body className="min-h-dvh">
        <Providers viewer={viewer} prefs={prefs}>
          {children}
        </Providers>
      </body>
    </html>
  );
}
