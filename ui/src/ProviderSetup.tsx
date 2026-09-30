import { createSignal, For, onMount, Show } from "solid-js";
import { api } from "./api";
import type { RecordData } from "./api";

export default function ProviderSetup(props: {
  profiles: RecordData[];
  changed: () => Promise<void>;
  beforeLogin?: () => void;
  selected?: (id: string) => void;
}) {
  const [status, setStatus] = createSignal<RecordData>();
  const [provider, setProvider] = createSignal("chatgpt");
  const [profile, setProfile] = createSignal("personal-chatgpt");
  const [model, setModel] = createSignal("");
  const [models, setModels] = createSignal<RecordData[]>([]);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal("");
  const account = () => props.profiles.find((p) => p.id === profile());
  async function work(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function loadModels() {
    const result = await api<RecordData[]>(`profiles/${profile()}/models`);
    setModels(result);
    if (result.length && !model()) setModel(result[0].id);
  }
  onMount(() =>
    work(async () => {
      setStatus(await api("providers"));
      const saved = props.profiles.find(
        (p) =>
          p.provider === "chatgpt" &&
          ["ready", "model_required"].includes(p.status),
      );
      if (saved) {
        setProfile(saved.id);
        setModel(saved.model || "");
        await loadModels();
      }
    }),
  );
  async function save() {
    const saved = await api("profiles", "POST", {
      id: profile(),
      provider: provider(),
      model: model(),
      api_key_env: "OPENAI_API_KEY",
    });
    await props.changed();
    if (saved.status === "ready") props.selected?.(saved.id);
    else
      setError(
        "OPENAI_API_KEY is not available to the service. Set it in the terminal that starts Entryplug, then restart the service.",
      );
  }
  return (
    <div class="provider-setup">
      <h3>Connect a model</h3>
      <p>Sign in with ChatGPT, or use a separate OpenAI API key.</p>
      <div class="segmented" role="group" aria-label="Provider authentication">
        <button
          classList={{ selected: provider() === "chatgpt" }}
          onClick={() => {
            setProvider("chatgpt");
            setProfile("personal-chatgpt");
            setModel("");
          }}
        >
          ChatGPT account
        </button>
        <button
          classList={{ selected: provider() === "openai" }}
          onClick={() => {
            setProvider("openai");
            setProfile("openai-api");
            setModel("");
          }}
        >
          API key
        </button>
      </div>
      <Show when={status() && !status()!.openai_installed}>
        <div class="setup-notice">
          <strong>Install OpenAI support once</strong>
          <p>
            This installation includes the console, but not the login libraries.
            Stop Entryplug, run this in the project folder using your Entryplug
            environment, then start it again.
          </p>
          <pre>{status()!.install_command}</pre>
          <code>python -m entryplug ui</code>
        </div>
      </Show>
      <Show when={error()}>
        <p class="error" role="alert">
          {error()}
        </p>
      </Show>
      <label>
        Profile name
        <input
          value={profile()}
          onInput={(e) => setProfile(e.currentTarget.value)}
        />
      </label>
      <Show when={provider() === "chatgpt"}>
        <Show
          when={
            !account() ||
            !["ready", "model_required"].includes(account()!.status)
          }
          fallback={
            <p class="connection-ok">
              ✓ Signed in{account()!.email ? ` as ${account()!.email}` : ""}.
              Choose a model to finish.
            </p>
          }
        >
          <button
            class="chatgpt"
            disabled={busy() || !status()?.openai_installed}
            onClick={() =>
              work(async () => {
                const login = await api("auth/chatgpt/start", "POST", {
                  profile: profile(),
                });
                props.beforeLogin?.();
                location.assign(login.authorization_url);
              })
            }
          >
            Continue with ChatGPT
          </button>
          <p>
            Opens OpenAI sign-in in this tab. Availability depends on your
            account.
          </p>
        </Show>
      </Show>
      <Show when={provider() === "openai"}>
        <p>
          The service reads <code>OPENAI_API_KEY</code> from its environment.
          API usage is billed separately from ChatGPT.
        </p>
      </Show>
      <Show
        when={
          provider() === "openai" ||
          account()?.status === "model_required" ||
          account()?.status === "ready"
        }
      >
        <label>
          Model
          <input
            list="provider-models"
            placeholder={
              provider() === "chatgpt"
                ? "Load the models available to your account"
                : "Enter the model ID for your API account"
            }
            value={model()}
            onInput={(e) => setModel(e.currentTarget.value)}
          />
          <datalist id="provider-models">
            <For each={models()}>
              {(m) => <option value={m.id}>{m.name}</option>}
            </For>
          </datalist>
        </label>
        <div class="actions">
          <Show when={provider() === "chatgpt"}>
            <button disabled={busy()} onClick={() => work(loadModels)}>
              Load models
            </button>
          </Show>
          <button
            class="primary"
            disabled={busy() || !model() || !status()?.openai_installed}
            onClick={() => work(save)}
          >
            Save model profile
          </button>
        </div>
      </Show>
      <For each={props.profiles}>
        {(p) => (
          <div class="profile-row">
            <div>
              <strong>{p.id}</strong>
              <small>
                {p.provider} · {p.model || "No model selected"} ·{" "}
                {p.status.replaceAll("_", " ")}
              </small>
            </div>
            <button
              class="small"
              disabled={busy()}
              onClick={() =>
                work(async () => {
                  await api("auth/logout", "POST", { profile: p.id });
                  await props.changed();
                })
              }
            >
              Sign out
            </button>
          </div>
        )}
      </For>
    </div>
  );
}
