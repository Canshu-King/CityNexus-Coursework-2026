import { type RawFacts } from "../lib/rawFacts";
import { readErrorReceipt, receiptLabel } from "../lib/errorReceipt";

/**
 * The same three device-measured fields RawFactsPanel prints, mounted the way Q70 asks for them:
 * one element per contract field, `data-cnx-field` carrying the field's own name, and the value as
 * data -- so a gate can bind the anchor instead of scraping a `<pre>`. RawFactsPanel is left alone:
 * accepted gates already read its `data-raw-*` shape, and moving those readers is not this order's.
 *
 * Q71, the reason nothing else may be added here: no fragment of an upstream answer's text goes into
 * the DOM by any route, `data-*` included. Only what this device measured is mounted, and the
 * receipt line is the server's own value or the literal that says there is none (`lib/errorReceipt`).
 */
export function CnxMeasuredFacts({
  facts,
  zh,
  testId,
  envelope,
}: {
  facts: RawFacts;
  zh: boolean;
  testId: string;
  /** The parsed rejection envelope, when this device read one. Nothing is printed from its text. */
  envelope?: unknown;
}) {
  const receipt = readErrorReceipt(envelope);
  const fields: Array<{ name: string; value: string | null; label: string }> = [
    { name: "http_status", value: facts.http_status === null ? null : String(facts.http_status),
      label: zh ? "这台设备看到的回读：HTTP" : "This device's reading: HTTP" },
    { name: "content_type", value: facts.content_type, label: zh ? "回读的媒体类型" : "Media type read back" },
    { name: "body_bytes", value: facts.bytes === null ? null : String(facts.bytes),
      label: zh ? "这台设备数到的字节" : "Octets this device counted" },
  ];
  return (
    <>
      {fields.map((field) => (
        <span
          key={field.name}
          className="cnx-measured-fact"
          data-testid={`${testId}-${field.name}`}
          {...(field.value === null
            ? {}
            : { "data-cnx-field": field.name, "data-cnx-value": field.value })}
        >
          {field.value === null
            ? zh ? `${field.label}：这台设备没量到` : `${field.label}: this device measured none`
            : `${field.label} ${field.value}`}
        </span>
      ))}
      {/* Q68: the repeatable number is the server's, or the screen says there is none -- it never
          dresses a status or a code up as one, and no anchor is mounted without a value. */}
      <span
        className="cnx-measured-fact"
        data-testid={`${testId}-receipt`}
        {...receipt.attrs}
      >
        {`${receiptLabelOf(receipt.present, zh)} ${receipt.text}`}
      </span>
    </>
  );
}

function receiptLabelOf(present: boolean, zh: boolean): string {
  return present
    ? zh ? "服务器回执号" : "server receipt"
    : zh ? "服务器未给回执号" : "no server receipt";
}
