import autogen
from autogen.trace.nodes import node, GRAPH, ParameterNode
import string
import random
import numpy as np
from textwrap import dedent
from autogen.trace.optimizers import FunctionOptimizerV2, OPRO
from datasets import load_dataset

from typing import List
import copy
from autogen.trace.trace_ops import FunModule, trace_op, trace_class, TraceExecutionError
from autogen.trace.nodes import Node

import re
from tqdm import tqdm

from collie.constraints import Constraint, TargetLevel, Count, Relation

# download the data file
import os
import requests

import dill

from collie.constraint_renderer import ConstraintRenderer

"""
Best module for Collie is:
a Probe and a local modifier

Check if everything matches the constraint (Learn a verifier)
If not, propose local edit suggestions:
1. Identify the place to edit (Locate)
2. Suggest a change (Modify)

This pipeline is very easy to implement with this framework
"""

def download_file(url, file_path):
    """
    Downloads a file from the specified URL to a local file path.

    Parameters:
    - url: The URL of the file to download.
    - file_path: The local path where the file should be saved.
    """
    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(1024):
                f.write(chunk)
        print(f"File successfully downloaded to {file_path}")
    else:
        print(f"Failed to download file. Status code: {response.status_code}")


def init_data():
    # URL of the file to download
    url = "https://github.com/princeton-nlp/Collie/raw/master/data/all_data.dill"
    # Local path to save the file (change this to your desired path)
    file_path = "all_data.dill"

    file_exists = os.path.exists(file_path)
    if not file_exists:
        download_file(url, file_path)


def load_data(n=500, seed=42):
    with open("all_data.dill", "rb") as f:
        all_data = dill.load(f)

    # collect all examples (with unique prompts)
    # == this is the recommended evaluation ==
    all_examples = []
    prompts = {}
    for k in all_data.keys():
        for example in all_data[k]:
            if example["prompt"] not in prompts:
                all_examples.append(example)

    random.seed(seed)
    random.shuffle(all_examples)

    return all_examples[:n]


# Do an agent thing
# then do OPRO
# Trace we do adversarial learning
@trace_class
class Verifier:
    def forward(self, constraint_str_rep, answer, targets):
        results = self.verify_constraint(constraint_str_rep, answer, targets)
        if len(results) == 2:
            return results
        else:
            return False, "Error in verifying constraint"

    @trace_op(trainable=True, catch_execution_error=True, allow_external_dependencies=True)
    def verify_constraint(self, constraint_str_rep, answer, targets):
        """
        Args:
            constraint_str_rep: a string that has:
                Example: All(Constraint(
                            InputLevel(paragraph),
                            TargetLevel(sentence),
                            Transformation(ForEach(Position(-1))),
                            Relation(==),
                            Reduction(all)
                        ), Constraint(
                            InputLevel(paragraph),
                            TargetLevel(sentence),
                            Transformation(ForEach(Count())),
                            Relation(>=),
                            Reduction(all)
                        ))

                Level: deriving classes InputLevel (the basic unit of the input) and TargetLevel (the level for comparing to the target value); levels include 'character', 'word', 'sentence', etc
                Transformation: defines how the input text is modified into values comparable against the provided target value; it derives classes like Count, Position, ForEach, etc
                Logic: And, Or, All that can be used to combine constraints
                Relation: relation such as '==' or 'in' for compariing against the target value
                Reduction: when the target has multiple values, you need to specify how the transformed values from the input is reduced such as 'all', 'any', 'at least'
                Constraint: the main class for combining all the above specifications

            answer: the user's answer hoping to satisfy the constraint
            targets: these are a list of values provided to the constraint verifier. For example, All(Constraint(), Constraint()) will get 2 targets, these are inputs to the constraints.
                     For example, if the constraint requires word count, then target value can be 5, which means count for 5 words.

        Returns: Boolean value indicating whether the constraint is satisfied
                 Feedback, a string that provides feedback on the what the user did wrong
        """
        return True, "Good job!"


class LLMCallable:
    def __init__(self, config_list=None, max_tokens=1024, verbose=False):
        if config_list is None:
            config_list = autogen.config_list_from_json("OAI_CONFIG_LIST")
        self.llm = autogen.OpenAIWrapper(config_list=config_list)
        self.max_tokens = max_tokens
        self.verbose = verbose

    @trace_op(catch_execution_error=False)
    def call_llm(self, user_prompt):
        """
        Sends the constructed prompt (along with specified request) to an LLM.
        """
        system_prompt = "You are a helpful assistant.\n"
        if self.verbose not in (False, "output"):
            print("Prompt\n", system_prompt + user_prompt)

        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}]

        try:
            response = self.llm.create(
                messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception:
            response = self.llm.create(messages=messages, max_tokens=self.max_tokens)
        response = response.choices[0].message.content

        if self.verbose:
            print("LLM response:\n", response)
        return response

    def call_llm_iterate(self, user_prompt, response, feedback):
        """
        Sends the constructed prompt (along with specified request) to an LLM.
        """
        system_prompt = "You are a helpful assistant.\n"
        if self.verbose not in (False, "output"):
            print("Prompt\n", system_prompt + user_prompt)

        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": response},
                    {"role": "user", "content": feedback}]

        try:
            response = self.llm.create(
                messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception:
            response = self.llm.create(messages=messages, max_tokens=self.max_tokens)
        response = response.choices[0].message.content

        if self.verbose:
            print("LLM response:\n", response)
        return response

@trace_class
class Predict(LLMCallable):
    def __init__(self):
        super().__init__()

        self.demos = []
        self.prompt_template = dedent("""Please follow the constraint described in the task carefully. Here is the task:\n\n {}""")
        self.prompt_template = ParameterNode(self.prompt_template, trainable=False)

    @trace_op(trainable=True, catch_execution_error=True, allow_external_dependencies=True)
    def check_and_edit_response(self, constraint, response):
        """
        We check the constraint again and see if we can improve the response.
        Our procedure should be general enough to handle different types of constraints.
        We can add individual cases to handle each constraint.
        """
        import re
        response = response.strip()
        if "with paragraphs having the last sentence to be" in constraint:
            # we can add a local edit suggestion here
            pattern = re.compile(r"'([^']*)'")
            matches = pattern.search(response)
            valid_last_sentences = []
            for match in matches:
                valid_last_sentences.append(match)
            response = response + " " + valid_last_sentences[0]
        return response

    @trace_op(trainable=True, catch_execution_error=True, allow_external_dependencies=True)
    def create_prompt(self, prompt_template, constraint):
        """
        The function takes in a question and then add to the prompt for LLM to answer.
        Args:
            prompt_template: some guidance/hints/suggestions for LLM
            question: the question for the LLM to answer
        """
        return prompt_template.format(constraint)

    def forward(self, constraint):
        """
        question: text

        We read in a question and produces a response
        """
        user_prompt = self.create_prompt(self.prompt_template, constraint)
        response = self.call_llm(user_prompt)
        answer = self.check_and_edit_response(constraint, response)
        return answer

    def improve_with_feedback(self, question, response, feedback):
        user_prompt = self.create_prompt(self.prompt_template, question)
        response = self.call_llm_iterate(user_prompt, response, feedback)
        return response


def evaluate_dp(dp, examples):
    rewards = 0
    responses = []
    for example in tqdm(examples):
        try:
            response = dp.forward(example['prompt'])
            responses.append(response.data)
            constraint = example['constraint']
            correctness = constraint.check(response.data, example['targets'])
        except:
            correctness = False
            responses.append(None)

        rewards += correctness
    return rewards / len(examples), responses

def learn_predict(dp, optimizer, examples, val_examples, save_dir):
    cum_reward = 0
    epochs = 1

    optimizer.objective = "When update is made to parameters, make sure it's not fitting to a particular example." + optimizer.default_objective

    val_perfs = {}
    for epoch in range(epochs):
        for step, example in enumerate(tqdm(examples)):
            GRAPH.clear()
            # This is also online optimization
            # we have the opportunity to keep changing the function with each round of interaction
            try:
                response = dp.forward(example['prompt'])
                constraint = example['constraint']
                correctness = constraint.check(response.data, example['targets'])
                if correctness:
                    feedback = "The generated passage satisfies the constraint! No need to change anything."
                else:
                    feedback = "The generated passage does not satisfy constraint. Modify the prompt (but keep it general for all constraints).\n\n"
                    renderer = ConstraintRenderer(
                        constraint=constraint,  # Defined in step one
                        check_value=example['targets']
                    )
                    feedback += renderer.get_feedback(response.data)
                # feedback = "The generated passage satisfies the constraint! No need to change anything." if correctness else f"The generated passage does not satisfy constraint. Please modify the prompt and relevant parts of the program to help LLM produce the right passage."
                no_error = True
            except TraceExecutionError as e:
                # load in the previous best checkpoint, and try to optimize from that again
                # an error recovery mode (similar to MCTS!?)
                if len(val_perfs) > 0:
                    best_checkpoint = max(val_perfs, key=val_perfs.get)
                    dp.load(best_checkpoint)
                    try:
                        response = dp.forward(example['prompt'])
                        constraint = example['constraint']
                        correctness = constraint.check(response.data, example['targets'])
                        if correctness:
                            feedback = "The generated passage satisfies the constraint! No need to change anything."
                        else:
                            feedback = "The generated passage does not satisfy constraint.\n\n"
                            renderer = ConstraintRenderer(
                                constraint=constraint,  # Defined in step one
                                check_value=example['targets']
                            )
                            feedback += renderer.get_feedback(response.data)
                        no_error = True
                    except:
                        response = e.exception_node
                        feedback = response.data
                        correctness = False
                        no_error = False
                else:
                    response = e.exception_node
                    feedback = response.data
                    correctness = False
                    no_error = False

            print("Constraint:", example['prompt'])
            print("Expected example:", example['example'])
            print("Answer:", response.data)

            cum_reward += correctness
            checkpoint_name = f"{save_dir}/epoch_{epoch}_step_{step}.pkl"

            # if we can handle the case, no need to optimize
            if correctness:
                # evaluate on val examples
                try:
                    val_perf, _ = evaluate_dp(dp, val_examples)
                    val_perfs[checkpoint_name] = val_perf
                    dp.save(checkpoint_name)
                except:
                    pass

                continue

            # if val_perf is completely empty and there is no immediate error, we save two checkpoints
            if no_error and len(val_perfs) < 2:
                try:
                    val_perf, _ = evaluate_dp(dp, val_examples)
                    val_perfs[checkpoint_name] = val_perf
                    dp.save(checkpoint_name)
                except:
                    pass

            optimizer.zero_feedback()
            optimizer.backward(response, feedback)

            print(f"output={response.data}, feedback={feedback}, variables=\n")  # logging
            for p in optimizer.parameters:
                print(p.name, p.data)
            optimizer.step(verbose=False)

    # in the end, we select the best checkpoint on validation set
    # by here we have at least one checkpoint
    best_checkpoint = max(val_perfs, key=val_perfs.get)
    print(f"Best checkpoint: {best_checkpoint}", f"Val performance: {val_perfs[best_checkpoint]}")
    dp.load(best_checkpoint)

    checkpoint_name = f"{save_dir}/best_ckpt.pkl"
    dp.save(checkpoint_name)

    print(f"Total reward: {cum_reward}")
    return dp, cum_reward

def evaluate_dp_with_verifier(dp, verifier, examples):
    rewards = 0
    responses = []
    for example in tqdm(examples):
        try:
            max_try = 2
            while max_try > 0:
                prefix = "" if max_try == 3 else f"Previous attempt was not successful. Remaining try: {max_try}\n\n"
                response = dp.forward(prefix + example['prompt'])
                satisfied, feedback = verifier.forward(str(example['constraint']), response.data, example['targets'])
                if satisfied:
                    break
                else:
                    max_try -= 1

            responses.append(response.data)
            constraint = example['constraint']
            correctness = constraint.check(response.data, example['targets'])
        except:
            correctness = False
            responses.append(None)

        rewards += correctness
    return rewards / len(examples), responses

def learn_predict_with_verifier(dp, verifier, optimizer, verifier_optimizer, examples, val_examples, save_dir, op_name):
    cum_reward = 0
    epochs = 1

    val_perfs = {}
    for epoch in range(epochs):
        for step, example in enumerate(tqdm(examples)):
            GRAPH.clear()
            # This is also online optimization
            # we have the opportunity to keep changing the function with each round of interaction
            try:
                response = dp.forward(example['prompt'])
                constraint = example['constraint']
                correctness = constraint.check(response.data, example['targets'])
                feedback = "The generated passage satisfies the constraint! No need to change anything." if correctness else f"The generated passage does not satisfy constraint. Please modify the prompt and relevant parts of the program to help LLM produce the right passage."
                no_error = True
            except TraceExecutionError as e:
                # load in the previous best checkpoint, and try to optimize from that again
                # an error recovery mode (similar to MCTS!?)
                if len(val_perfs) > 0:
                    best_checkpoint = max(val_perfs, key=val_perfs.get)
                    dp.load(best_checkpoint)
                    try:
                        response = dp.forward(example['prompt'])
                        constraint = example['constraint']
                        correctness = constraint.check(response.data, example['targets'])
                        feedback = "The generated passage satisfies the constraint! No need to change anything." if correctness else f"The generated passage does not satisfy constraint. Please modify the prompt and relevant parts of the program to help LLM produce the right passage."
                        no_error = True
                    except:
                        response = e.exception_node
                        feedback = response.data
                        correctness = False
                        no_error = False
                else:
                    response = e.exception_node
                    feedback = response.data
                    correctness = False
                    no_error = False

            # we optimize the verifier as well, when there's no error from the LLM module
            if no_error:
                try:
                    verifier_response, _ = verifier.forward(str(example['constraint']), response.data, example['targets'])
                    if verifier_response == correctness:
                        verifier_correctness = True
                        verifier_feedback = "The verifier made the same decision as the constraint checker. No need to change anything."
                    else:
                        verifier_correctness = False
                        verifier_feedback = "The verifier made a different decision than the constraint checker. Please modify the verifier to help it make the right decision."

                    verifier_no_error = True
                except TraceExecutionError as e:
                    verifier_response = e.exception_node
                    verifier_feedback = verifier_response.data
                    verifier_correctness = False
                    verifier_no_error = False

                verifier_optimizer.zero_feedback()
                verifier_optimizer.backward(verifier_response, verifier_feedback)
                verifier_optimizer.step(verbose=False)

                if verifier_correctness:
                    verifier.save(f"{save_dir}/verifier_best_ckpt.pkl")

            print(example['prompt'])
            print("Expected example:", example['example'])
            print("Answer:", response.data)

            cum_reward += correctness
            checkpoint_name = f"{save_dir}/epoch_{epoch}_step_{step}.pkl"

            # if we can handle the case, no need to optimize
            if correctness:
                # evaluate on val examples
                try:
                    val_perf, _ = evaluate_dp(dp, val_examples)
                    val_perfs[checkpoint_name] = val_perf
                    dp.save(checkpoint_name)
                except:
                    pass

                continue

            # if val_perf is completely empty and there is no immediate error, we save two checkpoints
            if no_error and len(val_perfs) < 2:
                try:
                    val_perf, _ = evaluate_dp(dp, val_examples)
                    val_perfs[checkpoint_name] = val_perf
                    dp.save(checkpoint_name)
                except:
                    pass

            optimizer.zero_feedback()
            optimizer.backward(response, feedback)

            print(f"output={response.data}, feedback={feedback}, variables=\n")  # logging
            for p in optimizer.parameters:
                print(p.name, p.data)
            optimizer.step(verbose=False)

    # in the end, we select the best checkpoint on validation set
    # by here we have at least one checkpoint
    best_checkpoint = max(val_perfs, key=val_perfs.get)
    print(f"Best checkpoint: {best_checkpoint}", f"Val performance: {val_perfs[best_checkpoint]}")
    dp.load(best_checkpoint)

    checkpoint_name = f"{save_dir}/best_ckpt.pkl"
    dp.save(checkpoint_name)

    print(f"Total reward: {cum_reward}")
    return dp, cum_reward

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--optimizer", type=str, default="FunctionOptimizerV2", help="FunctionOptimizerV2|OPRO|no_op")
    parser.add_argument("--use_verifier", action="store_true", help="We add modules to add few-shot examples")
    parser.add_argument("--save_path", type=str, default="results/collie")
    parser.add_argument("--ckpt_save_name", type=str, default="trace_collie_ckpt")
    args = parser.parse_args()

    init_data()
    all_examples = load_data(n=350)

    if not os.path.exists(args.save_path):
        os.makedirs(args.save_path)

    trainset = all_examples[:80]
    valset = all_examples[80:100]
    test_set = all_examples[100:]

    stats = {}

    ckpt_save_name = args.ckpt_save_name
    ckpt_save_name = ckpt_save_name.replace("trace", "opro") if args.optimizer == "OPRO" else ckpt_save_name

    dp = Predict()
    if args.optimizer != 'no_op':
        if args.use_verifier:
            verifier = Verifier()
            optimizer = FunctionOptimizerV2(dp.parameters() + [dp.prompt_template],
                                            config_list=autogen.config_list_from_json("OAI_CONFIG_LIST"))
            verifier_optimizer = FunctionOptimizerV2(verifier.parameters(), config_list=autogen.config_list_from_json("OAI_CONFIG_LIST"))
            dp, rewards = learn_predict_with_verifier(dp, verifier, optimizer, verifier_optimizer, trainset, valset, ckpt_save_name)
        else:
            if args.optimizer == "OPRO":
                params = dp.parameters()
                for p in params:
                    p.trainable = False
                optimizer = OPRO([dp.prompt_template], config_list=autogen.config_list_from_json("OAI_CONFIG_LIST"))
            else:
                optimizer = FunctionOptimizerV2(dp.parameters() + [dp.prompt_template],
                                            config_list=autogen.config_list_from_json("OAI_CONFIG_LIST"))
            dp, rewards = learn_predict(dp, optimizer, trainset, valset, ckpt_save_name)

        stats['optimizer_log'] = optimizer.log
        stats['train_acc'] = rewards / len(trainset)

    stats["learned_prompt"] = dp.prompt_template.data
    stats["extract_answer"] = dp.parameters_dict()['extract_answer'].data
    stats["create_prompt"] = dp.parameters_dict()['create_prompt'].data

    print(stats["extract_answer"])

    val_acc, responses = evaluate_dp(dp, test_set)
    stats['val_acc'] = val_acc
    stats['val_responses'] = responses

    import pickle

    if args.optimizer == "FunctionOptimizerV2":
        save_name = "dp_agent" if not args.use_verifier else "dp_agent_with_verifier"
    elif args.optimizer == "no_op":
        save_name = "dp_agent_no_op" if not args.use_verifier else "dp_agent_with_verifier_no_op"
    else:
        save_name = "dp_agent_opro" if not args.use_verifier else "dp_agent_with_verifier_opro"

    with open(f"{args.save_path}/{save_name}.pkl", "wb") as f:
        pickle.dump(stats, f)