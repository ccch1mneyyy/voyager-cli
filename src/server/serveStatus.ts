/**
 * `GET /v1/status` as this serve answers it: the protocol's `StatusResponse` plus a few
 * fields only `voyager status` reads, so they stay out of the wire package.
 */
import type { StatusResponse } from '@voyager-crew/bridge-protocol';

import type { ActiveJobView } from '../jobs/jobRunner';

export type { ActiveJobView };

export interface ServeStatus extends StatusResponse {
  readonly activeJobs?: readonly ActiveJobView[];
}
