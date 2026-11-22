import { useQuery } from '@tanstack/react-query';

import { getJson } from './client';
import type { KbDiffView, KbVersionPage, KbVersionView } from './types';

/**
 * A KB version's content as the KB pages read it (docs/07). The api types `content` as an open
 * object (it is the whole KB), so each field is checked here rather than cast.
 */
export interface KbHypothesis {
  key: string;
  name: string;
  color: string;
  kind: string;
  description: string;
  /** source key → P(outcome | this hypothesis), in the source's outcome order. */
  likelihoods: Record<string, number[]>;
}

export interface KbSource {
  key: string;
  kind: 'PROBE' | 'EVIDENCE';
  mode: 'PASSIVE' | 'ACTIVE' | null;
  tier: string;
  outcomes: string[];
  description: string;
  expectedMinutes: number | null;
  /** The question's approved English wording, for sources the customer answers. */
  question: string | null;
  /** outcome key → the answer's wording on the phone, where the question bank has one. */
  answers: Record<string, string>;
}

export interface KbContent {
  name: string;
  description: string;
  hypotheses: KbHypothesis[];
  sources: KbSource[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

function ordered(order: unknown, map: Record<string, unknown>): string[] {
  const listed = strings(order).filter((k) => k in map);
  return [...listed, ...Object.keys(map).filter((k) => !listed.includes(k))];
}

export function parseKb(content: unknown): KbContent {
  const kb = isRecord(content) ? content : {};
  const hyps = isRecord(kb.hypotheses) ? kb.hypotheses : {};
  const srcs = isRecord(kb.sources) ? kb.sources : {};
  const bank = isRecord(kb.question_bank) ? kb.question_bank : {};
  const hypotheses = ordered(kb.hypothesis_order, hyps).map((key) => {
    const h = isRecord(hyps[key]) ? hyps[key] : {};
    const raw = isRecord(h.likelihoods) ? h.likelihoods : {};
    const likelihoods: Record<string, number[]> = {};
    for (const [source, row] of Object.entries(raw)) {
      const probs = isRecord(row) && Array.isArray(row.probs) ? row.probs : [];
      likelihoods[source] = probs.filter((p): p is number => typeof p === 'number');
    }
    return {
      key,
      name: str(h.display_name, key),
      color: str(h.color, '#8A8C92'),
      kind: str(h.kind),
      description: str(h.description).trim(),
      likelihoods,
    };
  });
  const sources = ordered(kb.source_order, srcs).map((key): KbSource => {
    const s = isRecord(srcs[key]) ? srcs[key] : {};
    const mode = s.mode === 'PASSIVE' || s.mode === 'ACTIVE' ? s.mode : null;
    const entry = isRecord(bank[key]) ? bank[key] : {};
    const variant = (Array.isArray(entry.variants) ? entry.variants : [])
      .filter(isRecord)
      .find((v) => v.language === 'en' && v.status === 'APPROVED');
    const answers: Record<string, string> = {};
    for (const o of Array.isArray(variant?.options) ? variant.options : []) {
      if (isRecord(o) && typeof o.key === 'string' && typeof o.text === 'string') {
        answers[o.key] ??= o.text;
      }
    }
    return {
      key,
      kind: s.kind === 'EVIDENCE' ? 'EVIDENCE' : 'PROBE',
      mode,
      tier: str(s.friction_tier, 'NONE'),
      outcomes: strings(s.outcomes),
      description: str(s.description).trim(),
      expectedMinutes: typeof s.expected_minutes === 'number' ? s.expected_minutes : null,
      question: s.question === true && variant ? str(variant.text) || null : null,
      answers,
    };
  });
  return {
    name: str(kb.name, 'Knowledge base'),
    description: str(kb.description).trim(),
    hypotheses,
    sources,
  };
}

/**
 * How strongly a source separates the hypotheses, in bits: the generalised Jensen–Shannon
 * divergence of its outcome distributions under each hypothesis, weighted equally. 0 means
 * every hypothesis predicts the same answers; log2(n) is the most it can be.
 */
export function separation(source: KbSource, hypotheses: readonly KbHypothesis[]): number {
  const rows = hypotheses
    .map((h) => h.likelihoods[source.key])
    .filter((r): r is number[] => Array.isArray(r) && r.length === source.outcomes.length);
  if (rows.length < 2) return 0;
  const entropy = (p: readonly number[]) =>
    -p.reduce((acc, x) => (x > 0 ? acc + x * Math.log2(x) : acc), 0);
  const mean = source.outcomes.map(
    (_, i) => rows.reduce((a, r) => a + (r[i] ?? 0), 0) / rows.length,
  );
  return entropy(mean) - rows.reduce((a, r) => a + entropy(r), 0) / rows.length;
}

/** The belief after one answer, from an even start over the KB's hypotheses (Bayes' rule). */
export function afterAnswer(
  source: KbSource,
  outcome: number,
  hypotheses: readonly KbHypothesis[],
): Record<string, number> {
  const raw = hypotheses.map((h) => h.likelihoods[source.key]?.[outcome] ?? 0);
  const total = raw.reduce((a, b) => a + b, 0) || 1;
  return Object.fromEntries(hypotheses.map((h, i) => [h.key, (raw[i] ?? 0) / total]));
}

export function useKbVersions() {
  return useQuery({
    queryKey: ['kb', 'versions'],
    queryFn: () => getJson<KbVersionPage>('/kb/versions?limit=200'),
  });
}

export function useKbContent(versionId: number | undefined) {
  return useQuery({
    queryKey: ['kb', 'version', versionId, 'content'],
    enabled: versionId !== undefined,
    staleTime: Infinity, // a stored version never changes
    queryFn: () => getJson<KbVersionView>(`/kb/versions/${String(versionId)}`),
    select: (v: KbVersionView) => ({ version: v, content: parseKb(v.content) }),
  });
}

/** What changed from a version's parent (docs/11 §6); only for versions that have one. */
export function useKbDiff(versionId: number | undefined, parentId: number | null | undefined) {
  return useQuery({
    queryKey: ['kb', 'version', versionId, 'diff'],
    enabled: versionId !== undefined && parentId !== null && parentId !== undefined,
    staleTime: Infinity,
    queryFn: () => getJson<KbDiffView>(`/kb/versions/${String(versionId)}/diff`),
  });
}
