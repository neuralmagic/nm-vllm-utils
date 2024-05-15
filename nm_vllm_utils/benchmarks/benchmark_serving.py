# flake8: noqa
# UPSTREAM SYNC: noqa is required for passing ruff run on nm-automation
"""Benchmark online serving throughput.

On the server side, run one of the following commands:
    vLLM OpenAI API server
    python -m vllm.entrypoints.openai.api_server \
        --model <your_model> --swap-space 16 \
        --disable-log-requests

    (TGI backend)
    ./launch_tgi_server.sh <your_model> <max_batch_total_tokens>

On the client side, run:
    python benchmarks/benchmark_serving.py \
        --backend <backend> \
        --model <your_model> \
        --dataset-name sharegpt \
        --dataset-path <path to dataset> \
        --request-rate <request_rate> \ # By default <request_rate> is inf
        --num-prompts <num_prompts> # By default <num_prompts> is 1000
"""
import argparse
import asyncio
import csv
import json
import os
import random
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncGenerator, Dict, List, Tuple

import numpy as np
from tqdm.asyncio import tqdm
from transformers import PreTrainedTokenizerBase  # type: ignore[import-untyped]
from vllm.transformers_utils.tokenizer import get_tokenizer

from nm_vllm_utils.benchmarks.backend_request_func import (
    ASYNC_REQUEST_FUNCS,
    RequestFuncInput,
    RequestFuncOutput,
)


@dataclass
class BenchmarkMetrics:
    completed: int
    total_input: int
    total_output: int
    request_throughput: float
    input_throughput: float
    output_throughput: float
    mean_ttft_ms: float
    median_ttft_ms: float
    p90_ttft_ms: float
    p95_ttft_ms: float
    p99_ttft_ms: float
    mean_tpot_ms: float
    median_tpot_ms: float
    p90_tpot_ms: float
    p95_tpot_ms: float
    p99_tpot_ms: float


def sample_sharegpt_requests(
    dataset_path: str,
    num_requests: int,
    tokenizer: PreTrainedTokenizerBase,
) -> List[Tuple[str, int, int]]:
    # Load the dataset.
    with open(dataset_path) as f:
        dataset = json.load(f)
    # Filter out the conversations with less than 2 turns.
    dataset = [data for data in dataset if len(data["conversations"]) >= 2]
    # Only keep the first two turns of each conversation.
    dataset = [
        (data["conversations"][0]["value"], data["conversations"][1]["value"])
        for data in dataset
    ]

    # some of these will be filtered out, so sample more than we need
    sampled_indices = random.sample(range(len(dataset)), int(num_requests * 1.2))
    dataset = [dataset[i] for i in sampled_indices]

    # Tokenize the prompts and completions.
    prompts = [prompt for prompt, _ in dataset]
    prompt_token_ids = tokenizer(prompts).input_ids
    completions = [completion for _, completion in dataset]
    completion_token_ids = tokenizer(completions).input_ids
    tokenized_dataset = []
    for i in range(len(dataset)):
        output_len = len(completion_token_ids[i])
        tokenized_dataset.append((prompts[i], prompt_token_ids[i], output_len))

    # Filter out too long sequences.
    filtered_dataset: List[Tuple[str, int, int]] = []
    for prompt, prompt_token_ids, output_len in tokenized_dataset:
        prompt_len = len(prompt_token_ids)
        if prompt_len < 4 or output_len < 4:
            # Prune too short sequences.
            # This is because TGI causes errors when the input or output length
            # is too short.
            continue
        if prompt_len > 1024 or prompt_len + output_len > 2048:
            # Prune too long sequences.
            continue
        filtered_dataset.append((prompt, prompt_len, output_len))

    # Sample the requests.
    sampled_requests = random.sample(filtered_dataset, num_requests)
    return sampled_requests


def sample_sonnet_requests(
    dataset_path: str,
    num_requests: int,
    input_len: int,
    output_len: int,
    prefix_len: int,
    tokenizer: PreTrainedTokenizerBase,
) -> List[Tuple[str, str, int, int]]:
    assert (
        input_len > prefix_len
    ), "'args.sonnet-input-len' must be greater than 'args.prefix-input-len'."

    # Load the dataset.
    with open(dataset_path) as f:
        poem_lines = f.readlines()

    # Tokenize the poem lines.
    poem_token_ids = tokenizer(poem_lines).input_ids
    average_poem_len = sum(len(token_ids) for token_ids in poem_token_ids) / len(
        poem_token_ids
    )

    # Base prefix for all requests.
    base_prompt = "Pick as many lines as you can from these poem lines:\n"
    base_message = [
        {
            "role": "user",
            "content": base_prompt,
        }
    ]
    base_prompt_formatted = tokenizer.apply_chat_template(
        base_message, add_generation_prompt=True, tokenize=False
    )
    base_prompt_offset = len(tokenizer(base_prompt_formatted).input_ids)

    assert (
        input_len > base_prompt_offset
    ), f"Please set 'args.sonnet-input-len' higher than {base_prompt_offset}."
    num_input_lines = round((input_len - base_prompt_offset) / average_poem_len)

    # First approximately `prefix_len` number of tokens in the
    # prompt are fixed poem lines.
    assert (
        prefix_len > base_prompt_offset
    ), f"Please set 'args.sonnet-prefix-len' higher than {base_prompt_offset}."

    num_prefix_lines = round((prefix_len - base_prompt_offset) / average_poem_len)
    prefix_lines = poem_lines[:num_prefix_lines]

    # Sample the rest of lines per request.
    sampled_requests: List[Tuple[str, str, int, int]] = []
    for _ in range(num_requests):
        sampled_lines = "".join(
            prefix_lines + random.sample(poem_lines, num_input_lines - num_prefix_lines)
        )

        prompt = f"{base_prompt}{sampled_lines}"
        message = [
            {
                "role": "user",
                "content": prompt,
            },
        ]
        prompt_formatted = tokenizer.apply_chat_template(
            message, add_generation_prompt=True, tokenize=False
        )
        prompt_len = len(tokenizer(prompt_formatted).input_ids)
        sampled_requests.append((prompt, prompt_formatted, prompt_len, output_len))

    return sampled_requests


async def get_request(
    input_requests: List[Tuple[str, int, int]],
    request_rate: float,
) -> AsyncGenerator[Tuple[str, int, int], None]:
    for request in iter(input_requests):
        yield request

        if request_rate == float("inf"):
            # If the request rate is infinity, then we don't need to wait.
            continue
        # Sample the request interval from the exponential distribution.
        interval = np.random.exponential(1.0 / request_rate)
        # The next request will be sent after the interval.
        await asyncio.sleep(interval)


def calculate_metrics(
    input_requests: List[Tuple[str, int, int]],
    outputs: List[RequestFuncOutput],
    dur_s: float,
    tokenizer: PreTrainedTokenizerBase,
) -> Tuple[BenchmarkMetrics, List[int]]:
    actual_output_lens = []
    total_input = 0
    completed = 0
    tpots = []
    ttfts = []
    for i in range(len(outputs)):
        if outputs[i].success:
            output_len = len(tokenizer(outputs[i].generated_text).input_ids)
            actual_output_lens.append(output_len)
            total_input += input_requests[i][1]
            if output_len > 1:
                tpots.append((outputs[i].latency - outputs[i].ttft) / (output_len - 1))
            ttfts.append(outputs[i].ttft)
            completed += 1
        else:
            actual_output_lens.append(0)

    metrics = BenchmarkMetrics(
        completed=completed,
        total_input=total_input,
        total_output=sum(actual_output_lens),
        request_throughput=completed / dur_s,
        input_throughput=total_input / dur_s,
        output_throughput=sum(actual_output_lens) / dur_s,
        mean_ttft_ms=float(
            np.mean(ttfts or 0) * 1000
        ),  # ttfts is empty if streaming is not supported by backend
        median_ttft_ms=float(np.median(ttfts or 0) * 1000),
        p90_ttft_ms=float(np.percentile(ttfts or 0, 90) * 1000),
        p95_ttft_ms=float(np.percentile(ttfts or 0, 95) * 1000),
        p99_ttft_ms=float(np.percentile(ttfts or 0, 99) * 1000),
        mean_tpot_ms=float(np.mean(tpots) * 1000),
        median_tpot_ms=float(np.median(tpots) * 1000),
        p90_tpot_ms=float(np.percentile(tpots, 90) * 1000),
        p95_tpot_ms=float(np.percentile(tpots, 95) * 1000),
        p99_tpot_ms=float(np.percentile(tpots, 99) * 1000),
    )

    return metrics, actual_output_lens


async def benchmark(
    args: argparse.Namespace,
    backend: str,
    api_url: str,
    model_id: str,
    tokenizer: PreTrainedTokenizerBase,
    input_requests: List[Tuple[str, int, int]],
    best_of: int,
    use_beam_search: bool,
    request_rate: float,
    disable_tqdm: bool,
) -> Dict:
    if backend in ASYNC_REQUEST_FUNCS:
        request_func = ASYNC_REQUEST_FUNCS.get(backend)
    else:
        raise ValueError(f"Unknown backend: {backend}")

    print(f"Traffic request rate: {request_rate}")

    pbar = None if disable_tqdm else tqdm(total=len(input_requests))

    benchmark_start_time = time.perf_counter()
    tasks = []
    async for request in get_request(input_requests, request_rate):
        prompt, prompt_len, output_len = request
        request_func_input = RequestFuncInput(
            model=model_id,
            prompt=prompt,
            api_url=api_url,
            prompt_len=prompt_len,
            output_len=output_len,
            best_of=best_of,
            use_beam_search=use_beam_search,
        )
        if request_func:
            tasks.append(
                asyncio.create_task(
                    request_func(request_func_input=request_func_input, pbar=pbar)
                )
            )
    outputs: List[RequestFuncOutput] = await asyncio.gather(*tasks)

    if not disable_tqdm and pbar:
        pbar.close()

    benchmark_duration = time.perf_counter() - benchmark_start_time

    metrics, actual_output_lens = calculate_metrics(
        input_requests=input_requests,
        outputs=outputs,
        dur_s=benchmark_duration,
        tokenizer=tokenizer,
    )

    request_prompt_length = [output.prompt_len for output in outputs]
    mean_request_prompt_length = float(np.mean(request_prompt_length or 0) * 1000)
    median_request_prompt = float(np.median(request_prompt_length or 0) * 1000)
    p90_request_prompt = float(np.percentile(request_prompt_length or 0, 90) * 1000)
    p95_request_prompt = float(np.percentile(request_prompt_length or 0, 95) * 1000)
    p99_request_prompt = float(np.percentile(request_prompt_length or 0, 99) * 1000)

    mean_request_generation_length = float(np.mean(actual_output_lens or 0) * 1000)
    median_request_generation = float(np.median(actual_output_lens or 0) * 1000)
    p90_request_generation = float(np.percentile(actual_output_lens or 0, 90) * 1000)
    p95_request_generation = float(np.percentile(actual_output_lens or 0, 95) * 1000)
    p99_request_generation = float(np.percentile(actual_output_lens or 0, 99) * 1000)

    e2e_latency = [output.processing_time for output in outputs]
    mean_e2e_latency = float(np.mean(e2e_latency or 0) * 1000)
    median_e2e_latency = float(np.median(e2e_latency or 0) * 1000)
    p90_e2e_latency = float(np.percentile(e2e_latency or 0, 90) * 1000)
    p95_e2e_latency = float(np.percentile(e2e_latency or 0, 95) * 1000)
    p99_e2e_latency = float(np.percentile(e2e_latency or 0, 99) * 1000)

    print("\033[1mWorkload report: \033[0m \n")
    print(
        f"\033[1mServer details: \033[0m Host URL: \033[4m{args.host}\033[0m  Port: {args.port} IP Address: Route: {args.endpoint} Request Payload Template: Benchmark Duration (s): {benchmark_duration}"
    )
    print(f"\033[1mModel details: \033[0m Name: {args.model}")
    print(
        f"\033[1mTask details: \033[0m Dataset: {args.dataset} Task: {args.task} Median Prefill Time {metrics.median_ttft_ms} (ms) Median Decode Time {metrics.median_tpot_ms} (ms) \n"
    )

    print(
        "\033[1mRequest Details: \033[0m                      \033[4mRequest Prompt Length (toks)\033[0m           "
        " \033[4mRequest Generation Length (toks)\033[0m"
    )
    print(
        f"RPS = {len(outputs)/benchmark_duration}                              Mean: {mean_request_prompt_length}                                  Mean: {mean_request_generation_length}"
    )
    print(
        f"Hourly Active Users: {args.hourly_users}              p50: {median_request_prompt}                                    p50: {median_request_generation}"
    )
    print(
        f"Total Requests: {len(outputs)}                    p90: {p90_request_prompt}                                   p90: {p90_request_generation}"
    )
    print(
        f"Completed Requests: {metrics.completed}                p95: {p95_request_prompt}                                   p95: {p95_request_generation}"
    )
    print(
        f"Successfully Requests: {metrics.completed}             p99: {p99_request_prompt}                                   p99: {p99_request_generation}"
    )
    print(f"Failed Requests: {len(outputs) - metrics.completed} \n")

    print("\033[1mSummary Workload Metrics: \033[0m \n")
    print(
        "\033[4mE2E Latency (s)\033[0m     \033[4mThroughput (toks/s)\033[0m "
        "  \033[4mTime To First Token (TTFT) (ms)\033[0m     \033[4mTime Per Output Token (TPOT) (ms)\033[0m"
    )
    print(
        f"Mean: {mean_e2e_latency}               Mean: {metrics.output_throughput}                Mean: {metrics.mean_ttft_ms}                              Mean: {metrics.mean_tpot_ms}"
    )
    print(
        f"p50: {median_e2e_latency}                                     p50: {metrics.median_ttft_ms}                               p50: {metrics.median_tpot_ms}"
    )
    print(
        f"p90: {p90_e2e_latency}                                     p90: {metrics.p90_ttft_ms}                               p90: {metrics.p90_tpot_ms}"
    )
    print(
        f"p95: {p95_e2e_latency}                                     p95: {metrics.p95_ttft_ms}                               p95: {metrics.p95_tpot_ms}"
    )
    print(
        f"p99: {p99_e2e_latency}                                     p99: {metrics.p99_ttft_ms}                                p99:  {metrics.p99_tpot_ms}"
    )

    print(
        "\n \033[1mTo further inspect the metrics from this workload, you may view "
        "the generated .csv files in the Workload_Output local directory.\033[0m"
    )

    result = {
        "duration": benchmark_duration,
        "completed": metrics.completed,
        "total_input_tokens": metrics.total_input,
        "total_output_tokens": metrics.total_output,
        "request_throughput": metrics.request_throughput,
        "input_throughput": metrics.input_throughput,
        "output_throughput": metrics.output_throughput,
        "mean_ttft_ms": metrics.mean_ttft_ms,
        "median_ttft_ms": metrics.median_ttft_ms,
        "p90_ttft_ms": metrics.p90_ttft_ms,
        "p95_ttft_ms": metrics.p95_ttft_ms,
        "p99_ttft_ms": metrics.p99_ttft_ms,
        "mean_tpot_ms": metrics.mean_tpot_ms,
        "median_tpot_ms": metrics.median_tpot_ms,
        "p90_tpot_ms": metrics.p90_tpot_ms,
        "p95_tpot_ms": metrics.p95_tpot_ms,
        "p99_tpot_ms": metrics.p99_tpot_ms,
        "input_lens": [output.prompt_len for output in outputs],
        "output_lens": actual_output_lens,
        "ttfts": [output.ttft for output in outputs],
        "itls": [output.itl for output in outputs],
        "generated_texts": [output.generated_text for output in outputs],
        "errors": [output.error for output in outputs],
    }
    return result


def main(args: argparse.Namespace) -> None:
    print(args)
    random.seed(args.seed)
    np.random.seed(args.seed)

    backend = args.backend
    model_id = args.model
    tokenizer_id = args.tokenizer if args.tokenizer is not None else args.model

    if args.base_url is not None:
        api_url = f"{args.base_url}{args.endpoint}"
    else:
        api_url = f"http://{args.host}:{args.port}{args.endpoint}"

    tokenizer = get_tokenizer(tokenizer_id, trust_remote_code=args.trust_remote_code)

    if args.dataset is not None:
        warnings.warn(
            "The '--dataset' argument will be deprecated in the next "
            "release. Please use '--dataset-name' and "
            "'--dataset-path' in the future runs.",
            stacklevel=2,
        )
        input_requests = sample_sharegpt_requests(
            dataset_path=args.dataset,
            num_requests=args.num_prompts,
            tokenizer=tokenizer,
        )

    elif args.dataset_name == "sharegpt":
        input_requests = sample_sharegpt_requests(
            dataset_path=args.dataset_path,
            num_requests=args.num_prompts,
            tokenizer=tokenizer,
        )

    elif args.dataset_name == "sonnet":
        # Do not format the prompt, pass to message directly
        if args.backend == "openai-chat":
            input_request = sample_sonnet_requests(
                dataset_path=args.dataset_path,
                num_requests=args.num_prompts,
                input_len=args.sonnet_input_len,
                output_len=args.sonnet_output_len,
                prefix_len=args.sonnet_prefix_len,
                tokenizer=tokenizer,
            )
            input_requests = [
                (prompt, prompt_len, output_len)
                for prompt, prompt_formatted, prompt_len, output_len in input_request
            ]
        else:
            assert (
                tokenizer.chat_template or tokenizer.default_chat_template
            ), "Tokenizer/model must have chat template for sonnet dataset."
            input_request = sample_sonnet_requests(
                dataset_path=args.dataset_path,
                num_requests=args.num_prompts,
                input_len=args.sonnet_input_len,
                output_len=args.sonnet_output_len,
                prefix_len=args.sonnet_prefix_len,
                tokenizer=tokenizer,
            )
            input_requests = [
                (prompt_formatted, prompt_len, output_len)
                for prompt, prompt_formatted, prompt_len, output_len in input_request
            ]

    else:
        raise ValueError(f"Unknown dataset: {args.dataset_name}")

    benchmark_result = asyncio.run(
        benchmark(
            args=args,
            backend=backend,
            api_url=api_url,
            model_id=model_id,
            tokenizer=tokenizer,
            input_requests=input_requests,
            best_of=args.best_of,
            use_beam_search=args.use_beam_search,
            request_rate=args.request_rate,
            disable_tqdm=args.disable_tqdm,
        )
    )

    # Save config and results to json
    if args.save_result:
        result_json = {}

        # Setup
        current_dt = datetime.now().strftime("%Y%m%d-%H%M%S")
        result_json["date"] = current_dt
        result_json["backend"] = backend
        result_json["model_id"] = model_id
        result_json["tokenizer_id"] = tokenizer_id
        result_json["best_of"] = args.best_of
        result_json["use_beam_search"] = args.use_beam_search
        result_json["num_prompts"] = args.num_prompts

        # Metadata
        if args.metadata:
            for item in args.metadata:
                if "=" in item:
                    kvstring = item.split("=")
                    result_json[kvstring[0].strip()] = kvstring[1].strip()
                else:
                    raise ValueError(
                        "Invalid metadata format. Please use KEY=VALUE format."
                    )

        # Traffic
        result_json["request_rate"] = (
            args.request_rate if args.request_rate < float("inf") else "inf"
        )

        # Merge with benchmark result
        result_json = {**result_json, **benchmark_result}

        # Save to file
        base_model_id = model_id.split("/")[-1]
        file_name = (
            f"{backend}-{args.request_rate}qps-{base_model_id}-{current_dt}.csv"  # noqa
        )
        if args.result_dir:
            file_name = os.path.join(args.result_dir, file_name)
        with open(file_name, "w", newline="") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=result_json.keys())
            writer.writeheader()
            writer.writerow(result_json)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Benchmark the online serving throughput."
    )
    parser.add_argument(
        "--backend",
        type=str,
        default="vllm",
        choices=list(ASYNC_REQUEST_FUNCS.keys()),
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Server or API base url if not using http host and port.",
    )
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--endpoint",
        type=str,
        default="/v1/completions",
        help="API endpoint.",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        help="Path to the ShareGPT dataset, will be deprecated in the " "next release.",
    )
    parser.add_argument(
        "--dataset-name",
        type=str,
        default="sharegpt",
        choices=["sharegpt", "sonnet"],
        help="Name of the dataset to benchmark on.",
    )
    parser.add_argument(
        "--dataset-path", type=str, default=None, help="Path to the dataset."
    )
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Name of the model.",
    )
    parser.add_argument(
        "--tokenizer",
        type=str,
        help="Name or path of the tokenizer, if not using the default tokenizer.",
    )
    parser.add_argument(
        "--best-of",
        type=int,
        default=1,
        help="Generates `best_of` sequences per prompt and " "returns the best one.",
    )
    parser.add_argument("--use-beam-search", action="store_true")
    parser.add_argument(
        "--num-prompts",
        type=int,
        default=1000,
        help="Number of prompts to process.",
    )
    parser.add_argument(
        "--sonnet-input-len",
        type=int,
        default=550,
        help="Number of input tokens per request, used only for sonnet dataset.",
    )
    parser.add_argument(
        "--sonnet-output-len",
        type=int,
        default=150,
        help="Number of output tokens per request, used only for sonnet dataset.",
    )
    parser.add_argument(
        "--sonnet-prefix-len",
        type=int,
        default=200,
        help="Number of prefix tokens per request, used only for sonnet dataset.",
    )
    parser.add_argument(
        "--request-rate",
        type=float,
        default=float("inf"),
        help="Number of requests per second. If this is inf, "
        "then all the requests are sent at time 0. "
        "Otherwise, we use Poisson process to synthesize "
        "the request arrival times.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--trust-remote-code",
        action="store_true",
        help="Trust remote code from huggingface",
    )
    parser.add_argument(
        "--disable-tqdm",
        action="store_true",
        help="Specify to disable tqdm progress bar.",
    )
    parser.add_argument(
        "--save-result",
        action="store_true",
        help="Specify to save benchmark results to a json file",
    )
    parser.add_argument(
        "--metadata",
        metavar="KEY=VALUE",
        nargs="*",
        help="Key-value pairs (e.g, --metadata version=0.3.3 tp=1) "
        "for metadata of this run to be saved in the result JSON file "
        "for record keeping purposes.",
    )
    parser.add_argument(
        "--result-dir",
        type=str,
        default=None,
        help="Specify directory to save benchmark json results."
        "If not specified, results are saved in the current directory.",
    )
    parser.add_argument(
        "--num-seconds",
        type=int,
        default=300,
        help="Specify the number of seconds for which the benchmark will run.",
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        choices=["chat", "rag", "summarization"],
        help="Specify the task type. If provided, a premade dataset relevant "
        "to the task will be pulled in.",
    )
    parser.add_argument(
        "--hourly-users",
        type=int,
        default=None,
        help="Specify the number of hourly active users. "
        "This needs to be provided if hourly active users information "
        "is not provided through 'request-rate'.",
    )

    args = parser.parse_args()
    main(args)
