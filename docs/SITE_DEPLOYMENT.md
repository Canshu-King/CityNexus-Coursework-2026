# Publishing the CityNexus Visual Walkthrough

**Prepared:** [docs/index.html](index.html) plus original screenshot assets in [docs/assets/](assets/).

This is an **interactive, English-language screenshot/evidence gallery**, not a live running AI travel application. The page links directly to original development files, browser-test records and experiment outcomes.

## Make it publicly viewable with GitHub Pages

1. Open [the public portfolio repository](https://github.com/Canshu-King/CityNexus-Coursework-2026).
2. Select **Settings → Pages**.
3. Under **Build and deployment**, select **Deploy from a branch**.
4. Select branch **main**, folder **/docs**, then **Save**.
5. When GitHub reports that deployment is complete, verify the site in a signed-out browser:

   **https://canshu-king.github.io/CityNexus-Coursework-2026/**

6. Check that the nine stages can be switched, the English descriptions load, the twelve screenshots display, and the buttons open the original GitHub evidence.

**This URL is only a proposed site location until Pages has actually deployed successfully.** We cannot claim it works merely because \`docs/index.html\` exists.

## Why use /docs?

The site references relative images in \`docs/assets/\`, so GitHub Pages can publish the whole viewer without exposing scripts, credential remnants or the entire source tree as the website. The original product source already remains public in the repository for inspection.

## Safety and interpretation

The page does not execute backend code, send location or call external AI providers. It uses user-clicked tabs to display historical screenshots. The labels distinguish LIVE PROVIDER, CONTROLLED BROWSER, SIMULATION and NOT VERIFIED evidence. Nothing on the page should be introduced as a live end-to-end system demo.

## If you prefer an alternative to Pages

GitHub already renders the [visual gallery](VISUAL_GALLERY.md), the [full portal](PROJECT_PORTAL.md) and the [120-second walkthrough](DEMO_STORYBOARD.md) as normal Markdown without deploying a site.
