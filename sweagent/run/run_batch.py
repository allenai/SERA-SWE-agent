"""
Run on a batch of instances/issues, e.g., SWE-bench.

[cyan][bold]=== BASIC OPTIONS ===[/bold][/cyan]

  -h --help           Show help text and exit
  --help_option      Print specific help text and exit

[cyan][bold]=== EXAMPLES ===[/bold][/cyan]

Basic usage: Run over a [bold][cyan]SWE-bench lite[/bold][/cyan][green]:

sweagent run-batch \\
    --instances.type swe_bench \\ # configure instances
    --instances.subset lite \\
    --instances.split dev  \\
    --instances.slice :50 \\     # first 50 instances
    --instances.shuffle=True \\  # shuffle instances (with fixed seed)
    --config config/default.yaml \\
    --agent.model.name gpt-4o  # configure model
[/green]

[cyan][bold]=== LOADING INSTANCES ===[/bold][/cyan]

[cyan][bold]From a file[/bold][/cyan] [green]--instances.type file --instances.path /path/to/file[/green].
[cyan][bold]From huggingface[/bold][/cyan] [green]--instances.type huggingface --instances.dataset_name=SWE_Bench_lite --instances.split=dev[/green].

All instance specifications support the [green]filter[/green], [green]slice[/green], and [green]shuffle[/green] options.
With [green]filter[/green], you can select specific instances, e.g., [green]--instances.filter='instance_id_1|instance_id_2'[/green].
"""

import getpass
import json
import logging
import os
import random
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from contextlib import ExitStack
from pathlib import Path
from subprocess import CalledProcessError
from typing import Self

import openai
import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.live import Live
from swerex.deployment.hooks.status import SetStatusDeploymentHook

from sweagent import TRAJECTORY_DIR
from sweagent.agent.agents import AgentConfig, get_agent_from_config
from sweagent.agent.hooks.status import SetStatusAgentHook
from sweagent.environment.hooks.status import SetStatusEnvironmentHook
from sweagent.environment.swe_env import SWEEnv
from sweagent.exceptions import ModelConfigurationError, TotalCostLimitExceededError
from sweagent.run._progress import RunBatchProgressManager
from sweagent.run.batch_instances import BatchInstance, BatchInstanceSourceConfig, SWEBenchInstances
from sweagent.run.common import BasicCLI, ConfigHelper, save_predictions
from sweagent.run.hooks.abstract import CombinedRunHooks, RunHook
from sweagent.run.hooks.apply_patch import SaveApplyPatchHook
from sweagent.run.merge_predictions import merge_predictions
from sweagent.run.run_single import RunSingleConfig
from sweagent.types import AgentRunResult
from sweagent.utils.config import load_environment_variables
from sweagent.utils.log import (
    add_file_handler,
    add_logger_names_to_stream_handlers,
    get_logger,
    register_thread_name,
    remove_file_handler,
    set_stream_handler_levels,
)

### Changes
from sweagent.run.sera_sweagent_utils import (
    pp_query, 
    pp_regex, 
    parse_trajectory
)
### Changes

class PipelinePromptsConfig(BaseSettings, cli_implicit_flags=False):
    # Synthetic PR generation prompts
    synthetic_traj_prompt: str = ""
    check_synthetic_traj_prompt: str = ""
    """
    Optional config for running the SVG/pipeline flow.
    If omitted (RunBatchConfig.pipeline is False/None), we run the normal SWE-agent flow.
    """

    # Rollout one bug type prompts
    rollout_one_prompts: list[str] = Field(default_factory=list)
    """
    A list of prompts/templates to use for the "rollout_one" stage (if your pipeline uses it).
    """

    # Real synthetic issues
    swebench_prs: dict = Field(default_factory=dict)
    """
    PRs to use as demonstration issues. Override with RunBatchConfig.pipeline_repo.
    """

    @classmethod
    def load(cls, path: str | Path):
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(data)  # strict type validation

class RunBatchConfig(BaseSettings, cli_implicit_flags=False):
    instances: BatchInstanceSourceConfig = Field(description="Instances to run.")
    agent: AgentConfig = Field(description="Agent options.")
    output_dir: Path = Field(default=Path("DEFAULT"), description="Output directory.")
    suffix: str = ""
    """Suffix to add to the output directory. Only used if `output_dir` is `DEFAULT`."""
    raise_exceptions: bool = False
    """Raise exceptions instead of skipping instances."""
    redo_existing: bool = False
    """Do not skip instances that already have a trajectory."""
    env_var_path: Path | None = None
    """Path to a .env file to load environment variables from."""
    num_workers: int = Field(default=1)
    """Number of parallel workers to use."""
    random_delay_multiplier: float = 0.3
    """We will wait for a random amount of time between 0 and `random_delay_multiplier`
    times the number of workers at the start of each instance. This is to avoid any
    potential race condition or issues with bottlenecks, e.g., when running on a platform
    with few CPUs that cannot handle the startup of all containers in time.
    """
    progress_bar: bool = True
    """Whether to show a progress bar. Progress bar is never shown for human models.
    Progress bar is always shown for multi-worker runs.
    """
    ### Changes ###
    # New input fields for SVG
    keep_id: str = ""
    skip_id: str = ""
    """
    Comma separated strings of instance ids/subids to keep or skip to run on subset of instances
    """

    pipeline: bool = False
    """
    Whether to run SVG, if False then normal SWE-agent is run.
    """

    pipeline_repo: str = ""
    """
    Name of swebench repo to specialize to OR a JSON file path containing list of custom PR issues.
    """

    pipeline_yaml: str = ""
    """
    Config defining prompts for SVG pipeline.
    """

    # pydantic config
    model_config = SettingsConfigDict(extra="forbid", env_prefix="SWE_AGENT_")

    def set_default_output_dir(self) -> None:
        # Needs to be called explicitly, because self._config_files will be setup
        # post-init.
        if self.output_dir == Path("DEFAULT"):
            user_id = getpass.getuser()
            source_id = self.instances.id
            try:
                model_id = self.agent.model.id  # type: ignore[attr-defined]
            except AttributeError:
                model_id = "unknown"
            config_file = getattr(self, "_config_files", ["no_config"])[0]
            if config_file != "no_config":
                config_file = Path(config_file).stem
            suffix = f"__{self.suffix}" if self.suffix else ""
            self.output_dir = TRAJECTORY_DIR / user_id / f"{config_file}__{model_id}___{source_id}{suffix}"

    @model_validator(mode="after")
    def evaluate_and_redo_existing(self) -> Self:
        if not isinstance(self.instances, SWEBenchInstances):
            return self
        if self.instances.evaluate and self.redo_existing:
            msg = (
                "Cannot evaluate and redo existing at the same time. This would cause invalid results, because "
                "after the first merge_preds gives you a preds.json, this file would be submitted to SB-CLI, causing"
                "evaluation of old instances, which could then not be overwritten by the new ones."
            )
            raise ValueError(msg)
        return self


class _BreakLoop(Exception):
    """Used for internal control flow"""


class RunBatch:
    def __init__(
        self,
        instances: list[BatchInstance],
        agent_config: AgentConfig,
        *,
        output_dir: Path = Path("."),
        hooks: list[RunHook] | None = None,
        raise_exceptions: bool = False,
        redo_existing: bool = False,
        num_workers: int = 1,
        progress_bar: bool = True,
        random_delay_multiplier: float = 0.3,
        ### Changes ###
        keep_id: str = "",
        skip_id: str = "",
        pipeline: bool = False,
        pipeline_repo: str = "",
        pipeline_yaml: str = ""
        ### Changes ###
    ):
        """Note: When initializing this class, make sure to add the hooks that are required by your actions.
        See `from_config` for an example.

        Args:
            hooks: If not specified, the default hooks will be used.
            num_workers: Number of parallel workers to use. Default is 1 (sequential execution).
            progress_bar: Whether to show a progress bar. Progress bar is never shown for human models.
                Progress bar is always shown for multi-worker runs.
            random_delay_multiplier: We will wait for a random amount of time between 0 and `random_delay_multiplier`
                times the number of workers at the start of each instance. This is to avoid any
                potential race conditions.
        """
        if self._model_id in ["human", "human_thought"] and num_workers > 1:
            msg = "Cannot run with human model in parallel"
            raise ValueError(msg)

        self.logger = get_logger("swea-run", emoji="🏃")
        add_file_handler(
            output_dir / "run_batch.log",
            id_="progress",
            filter=lambda name: "swea-run" in name or "config" in name,
        )
        self.instances = instances
        self.agent_config = agent_config
        self.output_dir = output_dir
        self._raise_exceptions = raise_exceptions
        self._chooks = CombinedRunHooks()
        self._redo_existing = redo_existing
        self._num_workers = min(num_workers, len(instances))
        for hook in hooks or [SaveApplyPatchHook(show_success_message=False)]:
            self.add_hook(hook)
        self._progress_manager = RunBatchProgressManager(
            num_instances=len(instances), yaml_report_path=output_dir / "run_batch_exit_statuses.yaml"
        )
        self._show_progress_bar = progress_bar
        self._random_delay_multiplier = random_delay_multiplier

        ### Changes ###
        # Logging and handling for new parameters
        # keep_id and skip_id specify tasks to run on/skip based on subword matches.
        # pipeline, pipeline_repo, and pipeline_prompts are handled as follows:
        # Case 1:
        #   SVG pipeline and pipeline_repo defined, which selects for a sepcific set of demonstration synthetic PRs to use.
        #   pipeline_repo can either be a keyword for a repository in SWE-Bench (`django`, `sympy`, etc.) or a JSON file with a list.
        # Case 2:
        #   SVG pipeline, but no pipeline_repo, which will use general SWE-Bench PRs as demonstration issues.
        # Case 3:
        #   No SVG pipeline, so do nothing.
        # We handle three cases for loading demonstration issues.
        self._keep_id = keep_id.split(",")
        self._skip_id = skip_id.split(",")
        self._pipeline = pipeline
        self._pipeline_repo = pipeline_repo
        self._pipeline_prompts = PipelinePromptsConfig.load(pipeline_yaml)
        
        if self._pipeline and self._pipeline_repo: # Case 1. Handle specific demonstration issues
            if os.path.isfile(self._pipeline_repo): # Handle file to load demonstration issues
                try:
                    with open(self._pipeline_repo, "r") as f:
                        self._demonstration_issues = json.load(f)
                except json.JSONDecodeError as e: # Handle failed load
                    print("Invalid json file for demonstration issues, using SWE Bench PRs instead")
                    self._demonstration_issues = [v for k, v in self._pipeline_prompts.swebench_prs.items()]
                if not self._demonstration_issues: # Handle empty file
                    self._demonstration_issues = [v for k, v in self._pipeline_prompts.swebench_prs.items()]
            else: # Handle using existing demonstration issue
                for k, v in self._pipeline_prompts.swebench_prs.items(): # Select demonstrations from a specific repository specified
                    self._demonstration_issues = [v for k, v in self._pipeline_prompts.swebench_prs.items() if self._pipeline_repo.lower() in k]
                if not self._demonstration_issues: # If no specific repository specified or invalid repository specified
                    print(f"{self._pipeline_repo} is either not a file or not a valid identifier in SWE Bench, setting demostration PRs to all SWE Bench PRs")
                    self._demonstration_issues = [v for k, v in self._pipeline_prompts.swebench_prs.items()]
        elif self._pipeline: # Case 2. Handle no specific demonstration issues specified
            self._demonstration_issues = [v for k, v in self._pipeline_prompts.swebench_prs.items()]
        else: # Case 3. Handle case of not using SVG pipeline (do nothing)
            self._demonstration_issues = None
        if self._demonstration_issues:
            print(f"{len(self._demonstration_issues)} demonstration issues loaded")
        if self._pipeline:
            print("Pipeline Prompts:")
            print(f"Create Synthetic Issue: {self._pipeline_prompts.synthetic_traj_prompt}")
            print(f"Check Trajectory Against Rollout One Prompt: {self._pipeline_prompts.check_synthetic_traj_prompt}")
            print(f"SVG Rollout One Prompt (first idx): {self._pipeline_prompts.rollout_one_prompts[0]}")
            print(f"Demonstration Issue (first idx): {self._demonstration_issues[0]}")
        ### Changes ###

    @property
    def _model_id(self) -> str:
        try:
            return self.agent_config.model.id  # type: ignore[attr-defined]
        except AttributeError:
            return "unknown"

    @classmethod
    def from_config(cls, config: RunBatchConfig) -> Self:
        load_environment_variables(config.env_var_path)
        config.set_default_output_dir()
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / "run_batch.config.yaml").write_text(yaml.dump(config.model_dump_json(), indent=2))
        logger = get_logger("run", emoji="🏃")
        logger.debug("Loading instances from %s", f"{config.instances!r}")
        instances = config.instances.get_instance_configs()
        logger.info("Loaded %d instances", len(instances))
        if not instances:
            msg = (
                "No instances to run. Here are a few things to check:\n"
                "- With huggingface data: Check that you have the right split (test or dev)\n"
                "- Check your filter does not exclude all instances (check the info log messages)"
            )
            raise ValueError(msg)
        logger.debug("The first instance is %s", f"{instances[0]!r}")
        rb = cls(
            instances=instances,
            agent_config=config.agent,
            output_dir=config.output_dir,
            raise_exceptions=config.raise_exceptions,
            redo_existing=config.redo_existing,
            num_workers=config.num_workers,
            progress_bar=config.progress_bar,
            random_delay_multiplier=config.random_delay_multiplier,
            ### Changes
            keep_id=config.keep_id,
            skip_id=config.skip_id,
            pipeline=config.pipeline,
            pipeline_repo=config.pipeline_repo,
            pipeline_yaml=config.pipeline_yaml,
            ### Changes
        )
        if isinstance(config.instances, SWEBenchInstances) and config.instances.evaluate:
            from sweagent.run.hooks.swe_bench_evaluate import SweBenchEvaluate

            rb.add_hook(
                SweBenchEvaluate(
                    output_dir=config.output_dir,
                    subset=config.instances.subset,
                    split=config.instances.split,
                    continuous_submission_every=30,
                )
            )
        return rb

    def add_hook(self, hook: RunHook) -> None:
        hook.on_init(run=self)
        self._chooks.add_hook(hook)

    def main(self) -> None:
        self.logger.info("Starting run. Find output files at %s", self.output_dir)
        self._chooks.on_start()

        if self._num_workers <= 1:
            self.main_single_worker()
        else:
            self.main_multi_worker()

        output_dirs = []
        for instance in self.instances:
            output_dirs.append(self.output_dir / instance.problem_statement.id)
        merge_predictions(output_dirs, self.output_dir / "preds.json")

        self._chooks.on_end()

    def main_single_worker(self) -> None:
        with ExitStack() as stack:
            # Conditionally add progress bar
            if self._model_id not in ["human", "human_thought"] and self._show_progress_bar:
                stack.enter_context(Live(self._progress_manager.render_group))
            for instance in self.instances:
                try:
                    self.run_instance(instance)
                except _BreakLoop:
                    self.logger.info("Stopping loop over instances")
                    break

    def main_multi_worker(self) -> None:
        add_logger_names_to_stream_handlers()
        # Set all stream handlers to WARNING and set everything where we want to have
        # more verbosity explicitly
        set_stream_handler_levels(logging.ERROR)
        self.logger.setLevel(logging.ERROR)  # type: ignore

        with Live(self._progress_manager.render_group):
            with ThreadPoolExecutor(max_workers=self._num_workers) as executor:
                tasks_to_process = {executor.submit(self.run_instance, instance) for instance in self.instances}
                
                ### Changes ###
                # We modify this to retry the trajectory based on failures related to OS, CPU, network instability.
                # We do this by returning a None from run_instance() on successes, and the instance being run on failures.
                try:
                    while tasks_to_process:
                        # Wait for at least one task in our set to finish.
                        # 'wait' returns two new sets without affecting the running tasks.
                        completed_tasks, pending_tasks = wait(tasks_to_process, return_when=FIRST_COMPLETED)

                        # 1. Process the results of the tasks that just finished.
                        for future in completed_tasks:
                            result = future.result()
                            if result is not None:
                                # If we get a new task, submit it and add the new future
                                # to the set of pending tasks.
                                print(f"Adding {result.problem_statement.id} to retry...")
                                new_future = executor.submit(self.run_instance, result)
                                pending_tasks.add(new_future)
                        # 2. For the next loop, our set of tasks to process is now the
                        #    set of tasks that were already pending plus any new ones we just added.
                        tasks_to_process = pending_tasks
                ### Changes ###
                except (KeyboardInterrupt, _BreakLoop):
                    msg = (
                        "Received keyboard interrupt, waiting for running instances "
                        "to finish, but cancelled everything else"
                    )
                    self.logger.info(msg)
                    executor.shutdown(wait=False, cancel_futures=True)
                finally:
                    self._progress_manager.print_report()

    def run_instance(self, instance: BatchInstance) -> None:
        ### Changes ###
        # Log as an error for easier debugging
        self.logger.error("Running on instance %s", instance.problem_statement.id)
        ### Changes ###

        register_thread_name(instance.problem_statement.id)
        self._add_instance_log_file_handlers(instance.problem_statement.id, multi_worker=self._num_workers > 1)
        # Let's add some randomness to avoid any potential race conditions or thundering herd
        if self._progress_manager.n_completed < self._num_workers:
            time.sleep(random.random() * self._random_delay_multiplier * (self._num_workers - 1))

        self._progress_manager.on_instance_start(instance.problem_statement.id)

        if self.should_skip(instance):
            self._progress_manager.on_instance_end(instance.problem_statement.id, exit_status="skipped")
            self._remove_instance_log_file_handlers(instance.problem_statement.id)
            return

        # Either catch and silence exception, or raise _BreakLoop to stop the loop
        # over the instances

        ### Changes ###
        # Implement logic for skip_id and keep_id, skipping or running on tasks based on instance id subword matching
        run_on_this = (self._skip_id[0] == "" or not any(id in instance.problem_statement.id for id in self._skip_id)) and \
                (self._keep_id[0] == "" or any(id in instance.problem_statement.id for id in self._keep_id))
        ### Changes ###

        ### Changes ###
        # We add top level exception handling to catch certain timeout errors and retry automatically.
        # These changes tie directly into main_multi_worker().
        return_instance_for_retry = False
        if "repo" in instance.problem_statement.extra_fields:
            instance.problem_statement.extra_fields.pop("repo")
        try:
            # Handle running SVG pipeline or normal SWE-agent.
            if run_on_this:
                if self._pipeline:
                    result = self._run_instance_pipeline(instance)
                else:
                    result = self._run_instance(instance)
        except KeyboardInterrupt:
            raise _BreakLoop
        except (SystemExit, ModelConfigurationError, TotalCostLimitExceededError) as e:
            if self._raise_exceptions:
                raise
            self.logger.critical(f"❌ Exiting because {e.__class__.__name__} was called")
            raise _BreakLoop
        except Exception as e:
            self.logger.error(traceback.format_exc())
            self.logger.error(f"❌ Failed on {instance.problem_statement.id}: {e}")
            self._progress_manager.on_uncaught_exception(instance.problem_statement.id, e)
            if self._raise_exceptions:
                raise
            # Define the exceptions to handle and retry on.
            if (isinstance(e, CalledProcessError) 
                or isinstance(e, ConnectionError) 
                or isinstance(e, HTTPError)
                or isinstance(e, APITimeoutError)
                or isinstance(e, ClientConnectorError)
                or isinstance (e, TimeoutError)):
                self.logger.error(f"Signalling to retry {instance.problem_statement.id}...")
                return_instance_for_retry = True
        else:
            if not run_on_this:
                self._progress_manager.on_instance_end(
                    instance.problem_statement.id, exit_status=None
                )
            else:
                self._progress_manager.on_instance_end(
                    instance.problem_statement.id, exit_status=result.info.get("exit_status", "unknown_exit")
                )        
    
        finally:
            self._progress_manager.update_exit_status_table()
            self._remove_instance_log_file_handlers(instance.problem_statement.id)
        if return_instance_for_retry:
            # Return the instance being run if we want to retry
            return instance
        else:
            # Return None if we do not want to retry
            return
        ### Changes ###

    ### Changes ###
    # Added helper for creating synthetic issues from demonstration PRs
    def create_synth_inst(self, system_prompt, prompt, steps, example_pr, base_url, base_model):
        retry_max = 10
        try:
            while True:
                try:
                    synth_pr = pp_regex(pp_query(base_url=base_url, model=base_model, system=system_prompt,
                                                    prompt=prompt,
                                                    api_key="",
                                                    args={"agent_steps": json.dumps(steps),
                                                            "demonstration_issue": example_pr}))
                    # Only exit if 1) successful regex 2) no more retries
                    if synth_pr or retry_max == 0:
                        break
                    else:
                        retry_max -= 1
                except Exception as e:
                    # Retry if its a rate limit thing
                    if "rate_limit_error" in str(e) or "overload" in str(e):
                        time.sleep(10)
                        retry_max -= 1
                        if retry_max == 0:
                            return None
                    else:
                        print(e)
                        return None
            # If regex fails, we return None
            if synth_pr is None:
                return None
            else:
                # Logging
                return synth_pr[0]
        except Exception as e:
            # Catch any remaining exceptions
            print(e)
            return None
    ### Changes ###

    ### Changes ###
    # This is the main SVG pipeline logic.
    # There are 4 key steps:
    # Step 1: Randomly sample a random, vague prompt to fix a bug for SVG Rollout 1
    # Step 2: Run Rollout 1
    # Step 3: Grade the rollout and generate a synthetic issue
    # Step 4: Finish if Rollout 1 addresses the vague prompt. Otherwise, retry.
    def _run_instance_pipeline(self, instance: BatchInstance) -> AgentRunResult:
        retries = 3
        for i in range(retries):
            print(f"{instance.problem_statement.id}: pipeline attempt {i+1}/{retries}")
            output_dir = Path(self.output_dir) / instance.problem_statement.id
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            # Add instance log handlers
            self._add_instance_log_file_handlers(
                instance.problem_statement.id,
                multi_worker=self._num_workers > 1
            )
            self.agent_config.name = f"{instance.problem_statement.id}"
            agent = get_agent_from_config(self.agent_config)
            single_run_replay_config = RunSingleConfig(
                agent=self.agent_config,
                problem_statement=instance.problem_statement,
                env=instance.env,
            )
            (output_dir / f"{instance.problem_statement.id}.config.yaml").write_text(
                yaml.dump(single_run_replay_config.model_dump(), indent=2)
            )
            agent.replay_config = single_run_replay_config  # type: ignore[attr-defined]
            agent.add_hook(SetStatusAgentHook(instance.problem_statement.id, self._progress_manager.update_instance_status))
            self._progress_manager.update_instance_status(instance.problem_statement.id, "Starting environment")
            instance.env.name = f"{instance.problem_statement.id}"
            env = SWEEnv.from_config(instance.env)
            env.add_hook(
                SetStatusEnvironmentHook(instance.problem_statement.id, self._progress_manager.update_instance_status)
            )
            env.deployment.add_hook(
                SetStatusDeploymentHook(instance.problem_statement.id, self._progress_manager.update_instance_status)
            )
            is_good_patch = False
            try:
                # Here are different prompts
                # Step 1
                agent.templates.instance_template = random.sample(self._pipeline_prompts.rollout_one_prompts, 1)[0]

                # We add handling to load a previous simulated trajectory in directly as context.
                extra_fields = instance.problem_statement.get_extra_fields()
                env.start()
                self._chooks.on_instance_start(index=0, env=env, problem_statement=instance.problem_statement)
                # Step 2
                result = agent.run(
                    problem_statement=instance.problem_statement,
                    env=env,
                    output_dir=output_dir
                )
                metadata_file = output_dir / f"{instance.problem_statement.id}.synth"
                is_good_patch_retry = 3
                steps_truncation = 0
                agent_trajectory = parse_trajectory(result.trajectory)
                while True:
                    try:
                        # Step 3
                        is_good_patch_response = pp_query(system="You are a helpful assistant who can analyze code.", 
                                                            prompt=self._pipeline_prompts.check_synthetic_traj_prompt,
                                                            model=self.agent_config.model.name,
                                                            base_url=self.agent_config.model.api_base,
                                                            api_key=self.agent_config.model.api_key, args={"agent_steps": json.dumps(agent_trajectory[steps_truncation:]),
                                                                                                            "initial_prompt": result.agent_history[1]["content"]}).lower()
                    except openai.BadRequestError as e:
                        print(f"Retrying: {e}")
                    parsed_is_good_patch = pp_regex(is_good_patch_response)
                    print(f"{instance.problem_statement.id}: {is_good_patch_response}")
                    if parsed_is_good_patch:
                        if parsed_is_good_patch[0].strip().lower() == "yes":
                            is_good_patch = True
                        break
                    else:
                        is_good_patch_retry -= 1
                        steps_truncation += len(agent_trajectory) // 4
                        if is_good_patch_retry == 0:
                            break
                # Step 4
                synth_pr = self.create_synth_inst(system_prompt="You are a helpful assistant who can analyze code.",
                                                    prompt=self._pipeline_prompts.synthetic_traj_prompt,
                                                    steps=agent_trajectory[steps_truncation:],
                                                    example_pr=random.sample(self._demonstration_issues, 1)[0],
                                                    base_url=self.agent_config.model.api_base,
                                                    base_model=self.agent_config.model.name)
                with open(metadata_file, "w") as f:
                    json.dump({"is_good_patch": is_good_patch, 
                            "synth_pr": synth_pr}, 
                            f, 
                            indent=4)
                    
            except Exception:
                # The actual handling is happening in `run_instance`, but we need to make sure that
                # we log it to the agent specific logger as well
                agent.logger.error(traceback.format_exc())  # type: ignore[attr-defined]
                raise
            finally:
                env.close()
            if is_good_patch:
                break
        save_predictions(self.output_dir, instance.problem_statement.id, result)
        self._chooks.on_instance_completed(result=result)
        return result
    ### Changes ###

    def _run_instance(self, instance: BatchInstance) -> AgentRunResult:
        output_dir = Path(self.output_dir) / instance.problem_statement.id
        output_dir.mkdir(parents=True, exist_ok=True)
        self.agent_config.name = f"{instance.problem_statement.id}"
        agent = get_agent_from_config(self.agent_config)
        single_run_replay_config = RunSingleConfig(
            agent=self.agent_config,
            problem_statement=instance.problem_statement,
            env=instance.env,
        )
        (output_dir / f"{instance.problem_statement.id}.config.yaml").write_text(
            yaml.dump(single_run_replay_config.model_dump_json(), indent=2)
        )
        agent.replay_config = single_run_replay_config  # type: ignore[attr-defined]
        agent.add_hook(SetStatusAgentHook(instance.problem_statement.id, self._progress_manager.update_instance_status))
        self._progress_manager.update_instance_status(instance.problem_statement.id, "Starting environment")
        instance.env.name = f"{instance.problem_statement.id}"
        env = SWEEnv.from_config(instance.env)
        env.add_hook(
            SetStatusEnvironmentHook(instance.problem_statement.id, self._progress_manager.update_instance_status)
        )
        env.deployment.add_hook(
            SetStatusDeploymentHook(instance.problem_statement.id, self._progress_manager.update_instance_status)
        )
        try:
            env.start()
            self._chooks.on_instance_start(index=0, env=env, problem_statement=instance.problem_statement)
            result = agent.run(
                problem_statement=instance.problem_statement,
                env=env,
                output_dir=output_dir,
            )
        except Exception:
            # The actual handling is happening in `run_instance`, but we need to make sure that
            # we log it to the agent specific logger as well
            agent.logger.error(traceback.format_exc())  # type: ignore[attr-defined]
            raise
        finally:
            env.close()
        save_predictions(self.output_dir, instance.problem_statement.id, result)
        self._chooks.on_instance_completed(result=result)
        return result

    def should_skip(self, instance: BatchInstance) -> bool | str:
        """Check if we should skip this instance.
        Returns previous exit status if the instance should be skipped.
        """
        if self._redo_existing:
            return False

        # Check if there's an existing trajectory for this instance
        log_path = self.output_dir / instance.problem_statement.id / (instance.problem_statement.id + ".traj")
        if not log_path.exists():
            return False

        content = log_path.read_text()
        if not content.strip():
            self.logger.warning("Found empty trajectory: %s. Removing.", log_path)
            log_path.unlink()
            return False

        try:
            data = json.loads(content)
            # If the trajectory has no exit status, it's incomplete and we will redo it
            exit_status = data["info"].get("exit_status", None)
            if exit_status == "early_exit" or exit_status is None:
                self.logger.warning(f"Found existing trajectory with no exit status: {log_path}. Removing.")
                log_path.unlink()
                return False
            ### Changes ###
            # If there was some error in the trajectory (e.g. API fails) then we rerun the instance
            if "exit_error" in exit_status:
                self.logger.warning(f"Found existing trajectory with exit error: {log_path}. Removing.")
                log_path.unlink()
                return False
            ### Changes ###
        except Exception as e:
            self.logger.error(f"Failed to check existing trajectory: {log_path}: {e}. Removing.")
            # If we can't check the trajectory, we will redo it
            log_path.unlink()
            return False

        # otherwise, we will skip it
        self.logger.info(f"⏭️ Skipping existing trajectory: {log_path}")
        return exit_status

    def _add_instance_log_file_handlers(self, instance_id: str, multi_worker: bool = False) -> None:
        filename_template = f"{instance_id}.{{level}}.log"
        for level in ["trace", "debug", "info"]:
            filter = instance_id if multi_worker else ""
            add_file_handler(
                self.output_dir / instance_id / filename_template.format(level=level),
                filter=filter,
                level=level,
                id_=f"{instance_id}-{level}",
            )

    def _remove_instance_log_file_handlers(self, instance_id: str) -> None:
        for level in ["trace", "debug", "info"]:
            remove_file_handler(f"{instance_id}-{level}")


def run_from_config(config: RunBatchConfig):
    RunBatch.from_config(config).main()


def run_from_cli(args: list[str] | None = None):
    if args is None:
        args = sys.argv[1:]
    assert __doc__ is not None
    help_text = (  # type: ignore
        __doc__ + "\n[cyan][bold]=== ALL THE OPTIONS ===[/bold][/cyan]\n\n" + ConfigHelper().get_help(RunBatchConfig)
    )
    run_from_config(BasicCLI(RunBatchConfig, help_text=help_text).get_config(args))  # type: ignore


if __name__ == "__main__":
    run_from_cli()