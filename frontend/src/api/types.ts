import type { components } from './schema';

/**
 * Names for the generated API types (docs/16, docs/18 §1): they come from backend/openapi.json
 * through `pnpm gen:api`, never by hand.
 */
type Schemas = components['schemas'];

/** `GET /auth/me`. */
export type Me = Schemas['MeView'];
/** `POST /auth/login`. */
export type LoginRequest = Schemas['LoginRequest'];
export type Role = Schemas['Role'];
export type Permission = Schemas['Permission'];
/** `GET /cases/{id}` and the rows of `GET /cases` (the M4 case stub, #97). */
export type CaseView = Schemas['CaseView'];
/** `GET /cases`: one keyset page of the case queue. */
export type CasePage = Schemas['CasePage'];
/** `GET /guardian/sessions`: one keyset page of the monitor's list (#98). */
export type GuardianSessionPage = Schemas['GuardianSessionPage'];
/** A row of `GET /guardian/sessions`. */
export type GuardianSessionSummary = Schemas['GuardianSessionSummary'];
/** `GET /guardian/sessions/{id}` (docs/16 `GuardianSessionView`). */
export type GuardianSessionView = Schemas['GuardianSessionView'];
export type ProbeRunView = Schemas['ProbeRunView'];
/** `GET /kb/versions/{id}`, with the version's content. */
export type KbVersionView = Schemas['KbVersionView'];
/** A Guardian session's lifecycle state (the `status` filter of `GET /guardian/sessions`). */
export type SessionStatus = Schemas['SessionStatus'];
/** How a Guardian session was started (the `mode` filter of `GET /guardian/sessions`). */
export type SessionMode = Schemas['SessionMode'];
/** `GET /kb/versions`: newest first, without the content. */
export type KbVersionPage = Schemas['KbVersionPage'];
/** `GET /kb/versions/{id}/diff`: what changed from the parent. */
export type KbDiffView = Schemas['KbDiffView'];
/** A KB version's lifecycle state (docs/11 §5). */
export type KbStatus = Schemas['KbStatus'];
/** `GET /cases/{id}`: the case with its console (docs/09 §2, #103) and evidence (#102). */
export type CaseDetail = Schemas['CaseDetail'];
/** A row of the console's "next best evidence" (docs/09 §2 item 2). */
export type EvidenceRow = Schemas['EvidenceRowView'];
/** A fetched evidence record: the evidence card's content (docs/09 §2 item 5). */
export type EvidenceRecord = Schemas['EvidenceRecordView'];
/** "What would change my mind" (docs/06 §12). */
export type Flip = Schemas['FlipView'];
/** The case queue's orders. */
export type CaseSort = Schemas['CaseSort'];
