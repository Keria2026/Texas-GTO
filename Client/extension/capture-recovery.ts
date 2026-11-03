import { allowedPage } from './capture.ts';
import type { Session } from './model.ts';

export interface CaptureIntent { source: string; tabId: number; raw: boolean }
export async function recoverCapture(intent: unknown, interrupted: Session[],
  getTab: (id: number) => Promise<{ url?: string }>, start: (id: number, url: string, raw: boolean) => Promise<void>): Promise<boolean> {
  if (!intent || typeof intent !== 'object') return false;
  const value = intent as CaptureIntent;
  const previous = interrupted.find(s => s.stream !== 'business' && s.id === value.source
    && s.tabId === value.tabId && s.raw === value.raw);
  if (!previous || !Number.isSafeInteger(value.tabId) || typeof value.raw !== 'boolean') return false;
  const tab = await getTab(value.tabId).catch(() => undefined);
  if (!tab?.url || !allowedPage(tab.url)) return false;
  await start(value.tabId, tab.url, value.raw);
  return true;
}
