// Raw file contents through Vite, so the test sees exactly what gets bundled.
const runtime = import.meta.glob<string>('./*.svg', { query: '?raw', import: 'default', eager: true });
const kit = import.meta.glob<string>('../../../brand-kit/*.svg', { query: '?raw', import: 'default', eager: true });
const svgs = Object.entries({ ...runtime, ...kit });
const read = (name: string): string => {
  const svg = runtime[`./${name}`];
  if (svg === undefined) throw new Error(`missing brand asset ${name}`);
  return svg;
};

describe('Velocity V2 brand assets', () => {
  it('ships the symbol, app icon and favicon; the kit has the dark symbol', () => {
    expect(Object.keys(kit)).toEqual(['../../../brand-kit/velocity-symbol-dark.svg']);
    expect(Object.keys(runtime).sort()).toEqual([
      './favicon.svg',
      './velocity-icon.svg',
      './velocity-symbol.svg',
    ]);
  });

  it.each(svgs)('%s is a clean, self-contained, square vector', (_name, svg) => {
    expect(svg).toMatch(/<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" width="(\d+)" height="\1" viewBox="0 0 \1 \1"/);
    expect(svg).not.toMatch(/<script|<foreignObject|<image|\son\w+=|javascript:/i);
    // Only the SVG namespace may be an absolute URL: no external fonts, images or links.
    expect(svg.replace('http://www.w3.org/2000/svg', '')).not.toMatch(/https?:|xlink:href|\shref=|@import|url\((?!#)/i);
    expect(svg).not.toMatch(/figma\.com|mcp\/asset/i);
    // No hidden leftovers from the Figma export.
    expect(svg).not.toMatch(/display:\s*none|visibility="hidden"|opacity="0"|preserveAspectRatio="none"|overflow="visible"/);
  });

  it('keeps the favicon identical to the app icon (one approved icon)', () => {
    const body = (name: string) => read(name).replace(/^<!--.*-->\n/, '');
    expect(body('favicon.svg')).toBe(body('velocity-icon.svg'));
  });

  it('uses only the original Light Glass palette colours', () => {
    const allowed = new Set(['#17352D', '#CBF765', '#EAF3C6', '#CDEAC6', '#A0CEC9', '#E9F9A5', '#FFFFFF']);
    for (const [, svg] of svgs) {
      for (const colour of svg.match(/#[0-9A-Fa-f]{6}\b/g) ?? []) expect(allowed).toContain(colour.toUpperCase());
    }
  });

  it('draws the symbol strokes fully inside the 256 viewBox (nothing clipped at any size)', () => {
    const svg = read('velocity-symbol.svg');
    for (const m of svg.matchAll(/<path d="([^"]+)"[^>]*stroke-width="(\d+)"/g)) {
      const nums = m[1]!.match(/-?\d+(\.\d+)?/g)!.map(Number);
      const half = Number(m[2]) / 2;
      for (const n of nums) {
        expect(n - half).toBeGreaterThanOrEqual(0);
        expect(n + half).toBeLessThanOrEqual(256);
      }
    }
  });
});
