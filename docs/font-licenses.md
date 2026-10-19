# Bundled dashboard fonts

API Watch serves these fonts from its own `/static/fonts/` directory. The browser does not contact a font CDN or an external font stylesheet. [`fonts.css`](../api_watch/static/fonts.css) declares the exact CSS families `Inter` and `JetBrains Mono` with `font-display: swap`, so text remains visible using the application's fallback fonts while font files load.

Both fonts retain their complete upstream glyph sets, including Latin, extended Latin, and Cyrillic coverage. Only upright variable faces are bundled. The two WOFF2 files total 465,868 bytes, approximately 455 KiB, before normal HTTP caching.

| Family | Local asset | Variable axes | Size | Upstream source |
| --- | --- | --- | --- | --- |
| Inter | `InterVariable.woff2` | Weight 100–900; optical size 14–32 | 352,240 bytes | [Official Inter repository, tag v4.1](https://github.com/rsms/inter/blob/v4.1/docs/font-files/InterVariable.woff2) |
| JetBrains Mono | `JetBrainsMonoVariable.woff2` | Weight 100–800 | 113,628 bytes | [Official JetBrains Mono repository, tag v2.304](https://github.com/JetBrains/JetBrainsMono/blob/v2.304/fonts/variable/JetBrainsMono%5Bwght%5D.ttf) |

Inter's bundled font name table reports `Version 4.001;git-9221beed3`; this is the binary supplied by the official `v4.1` source tag. JetBrains Mono's name table reports `Version 2.304`. Inter is copied as the upstream WOFF2 binary. JetBrains Mono is converted from the upstream variable TTF to WOFF2 using the same `TTFont(...); font.flavor = 'woff2'; font.save(...)` process as the project's [official webfont generator](https://github.com/JetBrains/JetBrainsMono/blob/master/scripts/generate_variable_webfonts.py). Glyphs, names, variation axes, and style metadata are preserved; the font is not subsetted or redesigned.

The one-time JetBrains Mono conversion used FontTools 4.67.0 with Brotli 1.2.0. These tools are not application runtime dependencies. Serving the committed WOFF2 assets requires no font build step.

## Licenses and copyright

Both font families are distributed under the **SIL Open Font License 1.1**. Their complete, unmodified upstream license and copyright notices are included alongside the font binaries:

- [Inter-OFL.txt](../api_watch/static/fonts/Inter-OFL.txt), obtained from the [official Inter license at v4.1](https://github.com/rsms/inter/blob/v4.1/LICENSE.txt). Copyright © 2016 The Inter Project Authors.
- [JetBrainsMono-OFL.txt](../api_watch/static/fonts/JetBrainsMono-OFL.txt), obtained from the [official JetBrains Mono license at v2.304](https://github.com/JetBrains/JetBrainsMono/blob/v2.304/OFL.txt). Copyright © 2020 The JetBrains Mono Project Authors.

The font licenses apply to the bundled font software independently of API Watch's application license. Retain these license files when redistributing the font assets.

## Validation

Both files were decoded successfully with FontTools. Their WOFF2 magic is `wOF2`, their family/subfamily metadata is upright Regular, and their `fvar` tables confirm the weight ranges declared in CSS. Inter contains 2,852 mapped Unicode code points; JetBrains Mono contains 1,363. Both cover printable ASCII, extended Latin characters, and the Russian Cyrillic alphabet used when displaying user-provided names.
