import { List } from "@raycast/api";
import { useEffect, useState } from "react";
import { callBrainApi } from "./client";

export default function SearchMemoryCommand() {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (!query.trim()) {
      setItems([]);
      return;
    }
    let active = true;
    setIsLoading(true);
    callBrainApi<{ direct_results?: any[] }>("/v1/recall", "POST", { query, n_results: 10 })
      .then((res) => active && setItems(res.direct_results || []))
      .catch(() => active && setItems([]))
      .finally(() => active && setIsLoading(false));
    return () => { active = false; };
  }, [query]);

  return (
    <List isLoading={isLoading} onSearchTextChange={setQuery} searchBarPlaceholder="Search verified memory atoms..." throttle>
      {items.map((it) => (
        <List.Item key={it.filename} title={it.claim || it.filename} subtitle={it.filename} />
      ))}
    </List>
  );
}
