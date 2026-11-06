import type { SVGProps } from "react";

const paths: Record<string, string> = {
  map: "M4 6l6-2 4 2 6-2v14l-6 2-4-2-6 2V6zm6-2v14m4-12v14",
  bed: "M3 18v-7m0 3h18M5 14v-4a2 2 0 012-2h4a2 2 0 012 2v4m0 0v-3a2 2 0 012-2h4a2 2 0 012 2v3M3 18h18",
  spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zm7 12l.7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7L19 15z",
  calendar: "M6 3v4m12-4v4M4 9h16M5 5h14a1 1 0 011 1v13H4V6a1 1 0 011-1z",
  ticket: "M4 7a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 010 4v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2a2 2 0 010-4V7zm6-2v12",
  user: "M12 12a4 4 0 100-8 4 4 0 000 8zm-7 8a7 7 0 0114 0",
  arrow: "M5 12h14m-6-6l6 6-6 6",
};

export function Icon({ name, ...props }: { name: string } & SVGProps<SVGSVGElement>) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...props}>
      <path d={paths[name] ?? "M4 12h16M12 4v16"} />
    </svg>
  );
}
