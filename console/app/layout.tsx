import type { Metadata, Viewport } from "next";
import { Providers } from "@/components/providers";
import { inter, literata } from "@/lib/fonts";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Console", template: "%s · Console Swann" },
  description: "Administration du corpus Swann : supervision, dépôts, fiches, alignements.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fdfcf9" },
    { media: "(prefers-color-scheme: dark)", color: "#1a1816" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="fr" suppressHydrationWarning className={`${inter.variable} ${literata.variable}`}>
      <body className="min-h-dvh">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
