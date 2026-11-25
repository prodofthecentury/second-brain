import { Action, ActionPanel, Form, showToast, Toast } from "@raycast/api";
import { callBrainApi } from "./client";

export default function QuickRememberCommand() {
  async function handleSubmit(values: { title: string; content: string }) {
    try {
      const res = await callBrainApi("/v1/remember", "POST", { ...values, source: "raycast" });
      await showToast({ style: Toast.Style.Success, title: `Saved ${res.filename || "note"}` });
    } catch (err: any) {
      await showToast({ style: Toast.Style.Failure, title: err?.message || "Failed" });
    }
  }
  return (
    <Form actions={<ActionPanel><Action.SubmitForm title="Remember Note" onSubmit={handleSubmit} /></ActionPanel>}>
      <Form.TextField id="title" title="Title" placeholder="Optional note title" />
      <Form.TextArea id="content" title="Content" placeholder="Markdown note..." />
    </Form>
  );
}
