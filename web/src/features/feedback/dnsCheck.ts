export type DnsRecord = {
  type: "CNAME" | "TXT";
  name: string;
  value: string;
  purpose: string;
};

export type DnsRecordStatus = "unknown" | "verified" | "missing" | "mismatch";

export type DnsRecordResult = {
  status: DnsRecordStatus;
  /** What was measured — empty only before anyone clicked Check DNS. */
  detail: string;
};

export function dnsRecordKey(record: Pick<DnsRecord, "type" | "name">): string {
  return `${record.type}-${record.name}`;
}

export function initialDnsResults(records: DnsRecord[]): Record<string, DnsRecordResult> {
  return Object.fromEntries(
    records.map((r) => [dnsRecordKey(r), { status: "unknown", detail: "Not checked yet" }]),
  );
}

/** Measure DNS records for the Setup tab. No backend endpoint exists yet (PRD-47 S13). */
export function measureDnsRecords(records: DnsRecord[]): Record<string, DnsRecordResult> {
  const detail = "Check ran — no DNS verification endpoint on this deployment yet";
  return Object.fromEntries(
    records.map((r) => [dnsRecordKey(r), { status: "unknown" as const, detail }]),
  );
}
