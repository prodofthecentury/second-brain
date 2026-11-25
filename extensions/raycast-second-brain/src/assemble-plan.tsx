import { Action, ActionPanel, Detail, Form, showToast, Toast } from "@raycast/api";
import { useState } from "react";
import { callBrainApi } from "./client";

export default function AssemblePlanCommand() {
  const [markdown, setMarkdown] = useState("");
  async function handleSubmit(values: { goal: string }) {
    try {
      const res = await callBrainApi<{ summary?: string; steps?: { instruction: string }[] }>("/v1/plan", "POST", {
        goal: values.goal,
        mode: "auto",
      });
      const lines = [`# ${values.goal}`, "", res.summary || "", "", ...(res.steps || []).map((s, i) => `${i + 1}. ${s.instruction}`)];
      setMarkdown(lines.join("\n"));
    } catch (err: any) {
      await showToast({ style: Toast.Style.Failure, title: err?.message || "Failed" });
    }
  }
  if (markdown) return <Detail markdown={markdown} />;
  return (
    <Form actions={<ActionPanel><Action.SubmitForm title="Assemble Grounded Plan" onSubmit={handleSubmit} /></ActionPanel>}>
      <Form.TextField id="goal" title="Goal" placeholder="What do you want to accomplish?" />
    </Form>
  );
}
