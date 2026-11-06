import { QueryClient } from "@tanstack/react-query";

export const citynexusQueryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false },
  },
});
