# Website deployment

The static website source is `docs/`. The primary address is https://arammayhem.pages.dev/.

Cloudflare Pages project **arammayhem** uses **Direct Upload**, with `main` as the production branch. With Wrangler installed and authenticated with Pages write access, publish from the repository root:

```sh
wrangler pages deploy docs --project-name arammayhem --branch main
```

If localhost OAuth callbacks are unavailable, Wrangler also supports `wrangler login --device`; review its requested scopes before authorizing. Keep credentials outside the repository.

Alternatively, use the Cloudflare dashboard:

1. Package only the contents of `docs/` in a ZIP. Put `index.html` at the ZIP root, alongside `styles.css`, `site.js`, `favicon.svg`, `.nojekyll`, and the `assets/` folder. Do not include the repository, application configuration, credentials, or local logs.
2. In the Cloudflare dashboard, open **Workers & Pages > arammayhem > Create deployment** and select **Production**.
3. Upload the ZIP and deploy. Verify the page, screenshots, and download link at https://arammayhem.pages.dev/.

A Git push does **not** automatically deploy to Cloudflare. Deploy again after website changes.

The existing GitHub Pages site remains a mirror at https://mrmegamango.github.io/mayhem-companion/, published from the `main` branch's `/docs` folder.
