"use client";

import { useRouter } from "next/navigation";
import { useId, useState } from "react";

interface FinderCompany {
  id: string;
  name: string;
  nse_symbol: string | null;
}

/** Type a name or NSE symbol: an exact match opens its scorecard, anything else
 * filters the companies list. */
export function CompanyFinderInput({ companies }: { companies: FinderCompany[] }) {
  const router = useRouter();
  const [value, setValue] = useState("");
  const listId = useId();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const query = value.trim();
    if (!query) return;
    const needle = query.toLowerCase();
    const match = companies.find(
      (c) => c.name.toLowerCase() === needle || c.nse_symbol?.toLowerCase() === needle,
    );
    router.push(match ? `/companies/${match.id}` : `/companies?q=${encodeURIComponent(query)}`);
  }

  return (
    <form role="search" onSubmit={submit} className="w-full sm:w-64">
      <label className="sr-only" htmlFor={`${listId}-input`}>
        Find a company
      </label>
      <input
        id={`${listId}-input`}
        list={listId}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="Find a company or NSE symbol"
        autoComplete="off"
        className="w-full border-0 border-b border-ink bg-transparent py-1.5 text-[14px] placeholder:text-muted focus:outline-none focus-visible:outline-2 focus-visible:outline-link"
      />
      <datalist id={listId}>
        {companies.map((c) => (
          <option key={c.id} value={c.nse_symbol ?? c.name}>
            {c.name}
          </option>
        ))}
      </datalist>
    </form>
  );
}
