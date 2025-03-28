from src.model.GPT import GPT
import random
import argparse
from tqdm import tqdm
from feedback import run_test, run_pylint
import os
from collections import defaultdict
from utils import FEEDBACK_TYPES, api_key, read_jsonl, write_jsonl, gen_solution
from template import build_gpt_prompt, build_repair_prompt


def single_round_fix_code(file_path, model_name, model_version, feedback, dataset, use_docstring, use_context,
                          use_persona, use_cot, use_few_shot, use_instructions):
    print(f"Evaluating file: {file_path}")
    fixed_list = []
    ques_list = read_jsonl(file_path)

    for ques in tqdm(ques_list, total=len(ques_list), desc='Fixing code'):
        fixed_results = []
        list_results = ques["false_results"]
        for result in list_results:
            prompt = build_repair_prompt(
                solution=result["generate_code"],
                feedback=result.get(feedback, None),
                docstring=ques.get('docstring', None) if use_docstring else None,
                context=ques.get('oracle_context', None) if use_context else None,
                is_persona=use_persona,
                is_cot=use_cot,
                is_few_shot=use_few_shot,
                is_instructions=use_instructions
            )
            fixed_code = gen_solution(model_name, model_version, prompt)
            fixed_results.append({
                "source": result["source"],
                "false_code": result["generate_code"],
                "fixed_code": fixed_code,
            })

        if dataset == "HumanEval":
            fixed_list.append({
                "task_id": ques["task_id"],
                "fixed_results": fixed_results,
                "test": ques["test"]
            })
        elif dataset == "CoderEval":
            fixed_list.append({
                "_id": ques["_id"],
                "fixed_results": fixed_results,
                "level": ques["level"],
                "oracle_context": ques["oracle_context"],
                "docstring": ques["docstring"],
            })
        else:
            raise ValueError(f"Invalid dataset: {dataset}")

    if all([use_docstring, use_context, use_persona, use_instructions]) and not any([use_cot, use_few_shot]):
        save_dir = os.path.join("results", model_name, dataset, f"single")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{model_version}_{feedback}.jsonl")
    else:
        save_dir = os.path.join("results/rq4-prompt")
        os.makedirs(save_dir, exist_ok=True)
        config_suffix = (
            f"doc_{int(use_docstring)}_ctx_{int(use_context)}_"
            f"persona_{int(use_persona)}_cot_{int(use_cot)}_fewshot_{int(use_few_shot)}_instr_{int(use_instructions)}"
        )
        print(config_suffix)
        save_path = os.path.join(save_dir, f"{model_version}_{feedback}_{config_suffix}.jsonl")
    write_jsonl(save_path, fixed_list)
    print(f"File saved to: {save_path}")


def multi_round_fix_code(file_path, model_name, model_version, feedback, dataset, max_rounds=3):
    fixed_list = []
    ques_list = read_jsonl(file_path)
    print(f"Evaluating file: {file_path}")
    for ques in tqdm(ques_list, total=len(ques_list), desc='Multi-Round Fixing code'):
        list_results = ques["false_results"]
        # sampled_candidates = random.sample(list_results, min(sample_size, len(list_results)))
        candidate_processes = []
        for i, candidate in enumerate(list_results):
            candidate_proc = {
                "id": i,
                "source": candidate["source"],
                # repair_history records the state of each round, the initial round (0) records the original code
                "repair_history": [{
                    "round": 0,
                    "generate_code": candidate["generate_code"],
                    "feedback": candidate.get(feedback, None),
                    "isTrue": False,
                }],
                "current_code": candidate["generate_code"]  # Current candidate code for subsequent fixes
            }
            candidate_processes.append(candidate_proc)
        active_candidates = candidate_processes[:]  # Set of candidates to be fixed (candidates that failed the test)
        current_round = 1

        while current_round <= max_rounds and active_candidates:
            next_active_candidates = []
            for candidate_proc in active_candidates:
                current_code = candidate_proc["current_code"]
                try:
                    feedback_mapping = {
                        "test_feedback": run_test(dataset, current_code, ques.get('_id', None),
                                                  ques.get('test', None))[1],
                        "compiler_feedback": run_pylint(current_code),
                        "human_feedback": GPT(api_key, "gpt-4o-mini",
                                              build_gpt_prompt(dataset, current_code, ques.get('docstring', None),
                                                               ques.get('oracle_context', None))).generation(),
                        "simple_feedback": "The code is wrong. Please fix it."
                    }

                    if current_round == 1:
                        current_feedback = candidate_proc["repair_history"][0]["feedback"]
                    else:
                        current_feedback = feedback_mapping[feedback]

                    prompt = build_repair_prompt(current_code, current_feedback, ques.get('docstring', None),
                                                 ques.get('oracle_context', None))
                    fixed_code = gen_solution(model_name, model_version, prompt)

                    if not fixed_code:
                        new_solution = {
                            "round": current_round,
                            "generate_code": "",
                            "feedback": current_feedback,
                            "isTrue": False,
                        }
                        candidate_proc["repair_history"].append(new_solution)
                        # Do not add the candidate to the next round
                        continue

                    new_exit_code, new_test_feedback = run_test(dataset, fixed_code, ques.get('_id', None),
                                                                ques.get('test', None))
                    new_solution = {
                        "round": current_round,
                        "generate_code": fixed_code,
                        "feedback": current_feedback,
                        "isTrue": new_exit_code in (0, 5),
                    }

                    # Append the repair results of the current round to the repair_history of this candidate
                    candidate_proc["repair_history"].append(new_solution)
                    # Update the current candidate code to the generated fix code
                    candidate_proc["current_code"] = fixed_code
                    # If this fix fails the test, the candidate is kept for the next round of fixes
                    if new_exit_code not in (0, 5):
                        next_active_candidates.append(candidate_proc)
                except Exception as e:
                    print(f"Error during round {current_round + 1} code generation: {e}")

            # Update pending fix candidates
            active_candidates = next_active_candidates
            current_round += 1

        # Save the result after deleting the current_code field in candidate_proc
        for candidate_proc in candidate_processes:
            if "current_code" in candidate_proc:
                del candidate_proc["current_code"]

        if dataset == "HumanEval":
            fixed_list.append({
                "task_id": ques["task_id"],
                "repair_results": candidate_processes,
                "test": ques["test"]
            })
        elif dataset == "CoderEval":
            fixed_list.append({
                "_id": ques["_id"],
                "repair_results": candidate_processes,
                "level": ques["level"],
                "oracle_context": ques["oracle_context"],
                "docstring": ques["docstring"]
            })
        else:
            raise ValueError(f"Invalid dataset: {dataset}")

    save_dir = os.path.join("../../results", model_name, dataset)
    save_path = os.path.join(save_dir, f"{model_version}_multi_round_{feedback}.jsonl")
    os.makedirs(save_dir, exist_ok=True)
    write_jsonl(save_path, fixed_list)
    print(f"Results saved to {save_path}")


def pass_rate_single_round(input_path, dataset):
    num_accept, num_tot = 0, 0
    print(f"Calculating score for {input_path}")
    eval_data = read_jsonl(input_path)

    for data in tqdm(eval_data, total=len(eval_data), desc='Calculating score'):
        for result in data["fixed_results"]:
            fixed_code = result['fixed_code']
            if fixed_code:
                num_tot += 1
                exit_code, test_feedback = run_test(dataset, fixed_code, data.get('_id', None),
                                                    data.get('test', None))
                result['isTrue'] = exit_code in (0, 5)
                if exit_code not in (0, 5):
                    result['test_feedback'] = test_feedback
                num_accept += result['isTrue']

    write_jsonl(input_path, eval_data)
    print(f"Score: {num_accept / num_tot * 100:.2f}")


def pass_rate_multi_round(input_path):
    pass_rate_per_round = defaultdict(int)
    total = 0
    print(f"Evaluating file:{input_path}")
    eval_data = read_jsonl(input_path)

    for ques in eval_data:
        for result in ques["repair_results"]:
            if all(record["generate_code"] for record in result["repair_history"]):
                total += 1
            for record in result["repair_history"]:
                if record["round"] not in pass_rate_per_round:
                    pass_rate_per_round[record["round"]] = 0
                if record["isTrue"]:
                    pass_rate_per_round[record["round"]] += 1

    sorted_rounds = sorted(pass_rate_per_round.keys())
    cumulative_passed = 0

    for round_num in sorted_rounds:
        cumulative_passed += pass_rate_per_round[round_num]
        pass_rate = cumulative_passed / total if total > 0 else 0
        print(f"Round {round_num}: Pass rate = {pass_rate:.2%}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, help="CoderEval or HumanEval")
    parser.add_argument('--model', type=str, required=True, help="Model name")
    parser.add_argument('--version', type=str, required=True, help="Model version")
    parser.add_argument('--feedback', type=str, required=True, choices=FEEDBACK_TYPES, help="Type of feedback")
    parser.add_argument('--function', type=str, required=True,
                        choices=['single_fix', 'single_score', 'multi_fix', 'multi_score'],
                        help="Function to run")
    parser.add_argument('--no_docstring', action='store_false', help="Whether to use docstring")
    parser.add_argument('--no_context', action='store_false', help="Whether to use context")
    parser.add_argument('--no_persona', action='store_false', help="Whether to use persona")
    parser.add_argument('--is_cot', action='store_true', help="Whether to use chain of thought")
    parser.add_argument('--is_few_shot', action='store_true', help="Whether to use few-shot")
    parser.add_argument('--no_instructions', action='store_false', help="Whether to use instructions")
    args = parser.parse_args()
    if args.function == 'single_fix':
        input_path = os.path.join("dataset", args.dataset, f"{args.dataset}_feedback.jsonl")
        single_round_fix_code(input_path, args.model, args.version, args.feedback, args.dataset, args.no_docstring,
                              args.no_context, args.no_persona, args.is_cot, args.is_few_shot, args.no_instructions)
    elif args.function == 'single_score':
        input_path = os.path.join("results", args.model, args.dataset, f"{args.version}_{args.feedback}.jsonl")
        pass_rate_single_round(input_path, args.dataset)
    elif args.function == 'multi_fix':
        input_path = os.path.join("dataset", args.dataset, f"{args.dataset}_feedback.jsonl")
        multi_round_fix_code(input_path, args.model, args.version, args.feedback, args.dataset)
    elif args.function == 'multi_score':
        input_path = os.path.join("results", args.model, args.dataset,
                                  f"{args.version}_multi_round_{args.feedback}.jsonl")
        pass_rate_multi_round(input_path)


if __name__ == "__main__":
    main()
