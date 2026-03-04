# SERA-SWE-agent
This repository contains the SWE-agent harness for SERA: Soft-Verified Efficient Repository Agents.

We make modifications in five files:
- A new utils file in `sweagent/run/sera_sweagent_utils.py`
- Major SVG pipeline logic in `sweagent/run/run_batch.py`
- Slightly modify return logic in `sweagent/agent/agents.py`
- Slightly modify AgentRunResult in `sweagent/types.py`
- Slightly modify tool implementation in `tools/diff_state/bin/_state_diff_state`

Changes are surrounded by `### Changes ###` comments throughout the codebase.

## Detailed Change Log

### `tools/diff_state/bin/_state_diff_state`
- **Python compatibility fix**: Replaced f-string with string concatenation in the `subprocess.run` call so the script is compatible with earlier Python versions.

### `sweagent/types.py`
- **New field on `AgentRunResult`**: Added an `agent_history: list` field to carry the full trajectory history forward from the agent run.

### `sweagent/agent/agents.py`
- **Extended return value**: The agent's return statement now includes `agent_history=self.history` so that the SVG pipeline can inspect the conversation history after a run.

### `sweagent/run/run_batch.py`
This file contains the bulk of the changes, implementing the SVG (Synthetic Validation/Grading) pipeline.

#### New configuration fields (`RunBatchConfig`)
| Field | Type | Description |
|-------|------|-------------|
| `keep_id` | `str` | Comma-separated instance IDs/sub-IDs to include (substring match) |
| `skip_id` | `str` | Comma-separated instance IDs/sub-IDs to exclude (substring match) |
| `pipeline` | `bool` | Enable the SVG pipeline (default: normal SWE-agent) |
| `pipeline_repo` | `str` | SWE-bench repo name or path to a JSON file of custom PR issues |
| `pipeline_yaml` | `str` | Config file defining prompts for the SVG pipeline |

#### Instance filtering (`run_instance`)
- Instances are filtered by `keep_id` / `skip_id` before execution, using substring matching on the instance ID.

#### Demonstration issue loading (`__init__`)
Three cases are handled when the pipeline is enabled:
1. **`pipeline=True` + `pipeline_repo` set** — loads demonstration issues from a JSON file or selects SWE-bench PRs matching the given repo name.
2. **`pipeline=True` + no `pipeline_repo`** — uses all available SWE-bench PRs as demonstrations.
3. **`pipeline=False`** — no demonstration issues are loaded (normal mode).

#### Retry logic (`main_multi_worker` / `run_instance`)
- `run_instance` now returns the instance object on transient failures (connection errors, timeouts, API errors, process errors) and `None` on success.
- `main_multi_worker` uses `concurrent.futures.wait` to detect completed tasks and resubmits any that returned an instance for retry.
- Instances with an `exit_error` status in a previous trajectory are automatically rerun instead of skipped.

#### SVG pipeline (`_run_instance_pipeline`)
The pipeline executes four steps per instance (with up to 3 retries):
1. **Step 1** — Randomly sample a vague bug-fix prompt for the first rollout.
2. **Step 2** — Run the agent on the instance with that prompt.
3. **Step 3** — Grade the rollout trajectory against the initial prompt to decide if the patch is good.
4. **Step 4** — If the patch is good, generate a synthetic issue from the trajectory using `create_synth_inst`; otherwise retry from Step 1.

#### Synthetic issue creation (`create_synth_inst`)
- Helper method that calls the LLM to produce a synthetic issue from an agent trajectory and a demonstration PR.
- Includes retry logic for rate-limit and overload errors (up to 10 retries with back-off).

### `sweagent/run/sera_sweagent_utils.py`
- New utility file providing helper functions (`pp_query`, `pp_regex`, `parse_trajectory`, `PipelinePromptsConfig`, etc.) used by the SVG pipeline.

<p align="center">
  <a href="https://swe-agent.com/latest/">
    <img src="assets/swe-agent-banner.png" alt="swe-agent.com" style="height: 7em" />
  </a>
</p>

<p align="center">
<a href="https://swe-agent.com/latest/"><img src="https://img.shields.io/badge/Docs-green?style=for-the-badge&logo=materialformkdocs&logoColor=white" alt="Docs"></a>
<a href="https://join.slack.com/t/swe-bench/shared_invite/zt-36pj9bu5s-o3_yXPZbaH2wVnxnss1EkQ"><img src="https://img.shields.io/badge/Slack-4A154B?style=for-the-badge&logo=slack&logoColor=white" alt="Slack"></a>
<a href="https://arxiv.org/abs/2405.15793"><img src="https://img.shields.io/badge/arxiv-2405.15793-red?style=for-the-badge&logo=arxiv&logoColor=white&labelColor=black" alt="arxiv 2405.15793"></a>
</p>


SWE-agent enables your language model of choice (e.g. GPT-4o or Claude Sonnet 4) to autonomously use tools to
[fix issues in real GitHub repositories](https://swe-agent.com/latest/usage/hello_world),
[find cybersecurity vulnerabilities](https://enigma-agent.com/), or
[perform any custom task](https://swe-agent.com/latest/usage/coding_challenges).

* ✅ **State of the art** on SWE-bench among open-source projects
* ✅ **Free-flowing & generalizable**: Leaves maximal agency to the LM
* ✅ **Configurable & fully documented**: Governed by a single `yaml` file
* ✅ **Made for research**: Simple & hackable by design

SWE-agent is built and maintained by researchers from Princeton University and Stanford University.

> [!note]
> Most of our current development effort is on [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent/),
> which will eventually supersede SWE-agent. It already matches the performance performance of SWE-agent, while being
> much simpler.
> See the [FAQ](https://mini-swe-agent.com/latest/faq/) about whether you should switch already.

## 📣 News

* July 24: [Mini-SWE-Agent](https://github.com/SWE-agent/mini-SWE-agent) achieves 65% on SWE-bench verified in 100 lines of python!
* May 2: [SWE-agent-LM-32b](https://github.com/SWE-bench/SWE-smith) achieves open-weights SOTA on SWE-bench
* Feb 28: [SWE-agent 1.0 + Claude 3.7 is SoTA on SWE-Bench full](https://x.com/KLieret/status/1895487966409298067)
* Feb 25: [SWE-agent 1.0 + Claude 3.7 is SoTA on SWE-bench verified](https://x.com/KLieret/status/1894408819670733158)
* Feb 13: [Releasing SWE-agent 1.0: SoTA on SWE-bench light & tons of new features](https://x.com/KLieret/status/1890048205448220849)
* Dec 7: [An interview with the SWE-agent & SWE-bench team](https://www.youtube.com/watch?v=fcr8WzeEXyk)

## 🚀 Get started!

👉 Try SWE-agent in your browser: [![Open in GitHub Codespaces](https://img.shields.io/badge/Open_in_GitHub_Codespaces-gray?logo=github)](https://codespaces.new/SWE-agent/SWE-agent) ([more information](https://swe-agent.com/latest/installation/codespaces/))

Read our [documentation][docs] to learn more:

* [Installation](https://swe-agent.com/latest/installation/source/)
* [Hello world from the command line](https://swe-agent.com/latest/usage/hello_world/)
* [Benchmarking on SWE-bench](https://swe-agent.com/latest/usage/batch_mode/)
* [Frequently Asked Questions](https://swe-agent.com/latest/faq/)

[docs]: https://swe-agent.com

## SWE-agent for offensive cybersecurity (EnIGMA) <a name="enigma"></a>

<img src="https://github.com/user-attachments/assets/84599168-11a7-4776-8a49-33dbf0758bb2" height="80px"></img>

[SWE-agent: EnIGMA][enigma] is a mode for solving offensive cybersecurity (capture the flag) challenges.
EnIGMA achieves state-of-the-art results on multiple cybersecurity benchmarks (see [leaderboard](https://enigma-agent.com/#results)).
Please use [SWE-agent 0.7](https://github.com/SWE-agent/SWE-agent/tree/v0.7) while we update EnIGMA for 1.0.

[enigma]: https://enigma-agent.com
[SWE-bench]: https://github.com/SWE-bench/SWE-bench
[nyu-ctf]: https://arxiv.org/abs/2406.05590

In addition, you might be interested in our other projects:


<div align="center">
  <a href="https://github.com/SWE-agent/mini-SWE-agent"><img src="docs/assets/mini_logo_text_below.svg" alt="Mini-SWE-Agent" height="120px"></a>
   &nbsp;&nbsp;
  <a href="https://github.com/SWE-agent/SWE-ReX"><img src="docs/assets/swerex_logo_text_below.svg" alt="SWE-ReX" height="120px"></a>
   &nbsp;&nbsp;
  <a href="https://github.com/SWE-bench/SWE-bench"><img src="docs/assets/swebench_logo_text_below.svg" alt="SWE-bench" height="120px"></a>
  &nbsp;&nbsp;
  <!-- <a href="https://github.com/SWE-agent/SWE-agent"><img src="docs/assets/sweagent_logo_text_below.svg" alt="SWE-agent" height="120px"></a> -->
  <a href="https://github.com/SWE-bench/SWE-smith"><img src="docs/assets/swesmith_logo_text_below.svg" alt="SWE-smith" height="120px"></a>
  &nbsp;&nbsp;
  <a href="https://github.com/SWE-bench/sb-cli"><img src="docs/assets/sbcli_logo_text_below.svg" alt="sb-cli" height="120px"></a>
</div>

## Contributions <a name="contributions"></a>

If you'd like to contribute to the codebase, we welcome [issues](https://github.com/SWE-agent/SWE-agent/issues) and [pull requests](https://github.com/SWE-agent/SWE-agent/pulls)! For larger code changes, we always encourage discussion in issues first.

## Citation & contact <a name="citation"></a>

SWE-agent is an academic project started at Princeton University by John Yang*, Carlos E. Jimenez*, Alexander Wettig, Kilian Lieret, Shunyu Yao, Karthik Narasimhan, and Ofir Press.
Contact person: [John Yang](https://john-b-yang.github.io/), [Carlos E. Jimenez](http://www.carlosejimenez.com/), and [Kilian Lieret](https://www.lieret.net/) (Email: johnby@stanford.edu, carlosej@cs.princeton.edu, kl5675@princeton.edu).

If you found this work helpful, please consider citing it using the following:

<details>
<summary> SWE-agent citation</summary>

```bibtex
@inproceedings{yang2024sweagent,
  title={{SWE}-agent: Agent-Computer Interfaces Enable Automated Software Engineering},
  author={John Yang and Carlos E Jimenez and Alexander Wettig and Kilian Lieret and Shunyu Yao and Karthik R Narasimhan and Ofir Press},
  booktitle={The Thirty-eighth Annual Conference on Neural Information Processing Systems},
  year={2024},
  url={https://arxiv.org/abs/2405.15793}
}
```
</details>

If you used the summarizer, interactive commands or the offensive cybersecurity capabilities in SWE-agent, please also consider citing:

<details>
<summary>EnIGMA citation</summary>

```bibtex
@misc{abramovich2024enigmaenhancedinteractivegenerative,
      title={EnIGMA: Enhanced Interactive Generative Model Agent for CTF Challenges},
      author={Talor Abramovich and Meet Udeshi and Minghao Shao and Kilian Lieret and Haoran Xi and Kimberly Milner and Sofija Jancheska and John Yang and Carlos E. Jimenez and Farshad Khorrami and Prashanth Krishnamurthy and Brendan Dolan-Gavitt and Muhammad Shafique and Karthik Narasimhan and Ramesh Karri and Ofir Press},
      year={2024},
      eprint={2409.16165},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2409.16165},
}
```
</details>


## 🪪 License <a name="license"></a>
MIT. Check `LICENSE`.


<div align="center">

[![Pytest](https://github.com/SWE-agent/SWE-agent/actions/workflows/pytest.yaml/badge.svg)](https://github.com/SWE-agent/SWE-agent/actions/workflows/pytest.yaml)
[![build-docs](https://github.com/SWE-agent/SWE-agent/actions/workflows/build-docs.yaml/badge.svg)](https://github.com/SWE-agent/SWE-agent/actions/workflows/build-docs.yaml)
[![codecov](https://codecov.io/gh/SWE-agent/SWE-agent/graph/badge.svg?token=18XAVDK365)](https://codecov.io/gh/SWE-agent/SWE-agent)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/SWE-agent/SWE-agent/main.svg)](https://results.pre-commit.ci/latest/github/SWE-agent/SWE-agent/main)
[![Markdown links](https://github.com/SWE-agent/SWE-agent/actions/workflows/check-links-periodic.yaml/badge.svg)](https://github.com/SWE-agent/SWE-agent/actions/workflows/check-links-periodic.yaml)

</div>
