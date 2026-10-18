{
  description = "Swann : bibliothèque de livres avec recherche vectorielle";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAllSystems (pkgs:
        let
          python = pkgs.python312;
        in
        {
          # Les dépendances Python (torch, sentence-transformers...) sont gérées
          # par uv dans ingest/ (pyproject.toml + uv.lock) ; Nix fournit
          # l'interpréteur, uv, les clients des services et les bibliothèques
          # système dont les wheels manylinux ont besoin sur NixOS.
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.uv
              pkgs.postgresql_17 # psql
              pkgs.dbmate # migrations (db/migrations)
              pkgs.ruff
              pkgs.nodejs_22 # liseuse (reader/)
              pkgs.pnpm
            ];

            env = {
              UV_PYTHON = "${python}/bin/python3.12";
              UV_PYTHON_DOWNLOADS = "never";
              DBMATE_MIGRATIONS_DIR = "db/migrations";
              DBMATE_NO_DUMP_SCHEMA = "true";
              # Tests E2E de la liseuse : navigateurs fournis par Nix (même
              # version que @playwright/test, figée dans reader/package.json)
              PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
              PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
              NEXT_TELEMETRY_DISABLED = "1";
            };

            # libstdc++ / zlib pour les wheels (numpy, torch, lxml...) ;
            # /run/opengl-driver/lib = pilote NVIDIA de NixOS (libcuda.so),
            # requis par torch pour utiliser le GPU.
            shellHook = ''
              # Triton (noyaux GPU de torch) cherche libcuda via /sbin/ldconfig,
              # absent sur NixOS : on lui donne le chemin du pilote.
              export TRITON_LIBCUDA_PATH=/run/opengl-driver/lib
              export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ]}:/run/opengl-driver/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
              if [ -f .env ]; then set -a; . ./.env; set +a; fi
              # dbmate lit DATABASE_URL ; sslmode=disable car Postgres local sans TLS.
              export DATABASE_URL=''${DATABASE_URL:-postgresql://swann:swann@localhost:5432/swann?sslmode=disable}
            '';
          };
        });
    };
}
