import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/",
    name: "Swann — liseuse",
    short_name: "Swann",
    description: "Lire les grandes œuvres, dans leur langue et en traduction.",
    lang: "fr",
    start_url: "/",
    scope: "/",
    display: "standalone",
    orientation: "any",
    background_color: "#fdfcf9",
    theme_color: "#fdfcf9",
    categories: ["books", "education"],
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icons/maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
      { src: "/icons/icon.svg", sizes: "any", type: "image/svg+xml" },
    ],
    shortcuts: [
      {
        name: "Continuer la lecture",
        short_name: "Continuer",
        url: "/library",
        icons: [{ src: "/icons/icon-192.png", sizes: "192x192" }],
      },
      { name: "Rechercher un passage", short_name: "Recherche", url: "/search" },
    ],
  };
}
