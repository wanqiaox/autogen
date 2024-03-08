# To run the example code, please install the following packages:
# pip install gurobipy
# pip install eventlet
# pip install pyomo

# source: https://github.com/microsoft/OptiGuide/blob/main/optiguide/optiguide.py
# https://github.com/microsoft/OptiGuide/blob/main/notebook/optiguide_example.ipynb

import re
from typing import Dict, List, Optional, Union

from autogen.agentchat import AssistantAgent
from autogen.agentchat.agent import Agent
from autogen.code_utils import extract_code
from termcolor import colored

from gurobipy import GRB
from eventlet.timeout import Timeout
from pyomo.opt import TerminationCondition

from autogen.trace import trace, compatibility

# %% System Messages
WRITER_SYSTEM_MSG = """You are a chatbot to:
(1) write Python code to answer users questions for supply chain-related coding
project;
(2) explain solutions from a {solver_software} Python solver.

--- SOURCE CODE ---
{source_code}

--- DOC STR ---
{doc_str}
---

Here are some example questions and their answers and codes:
--- EXAMPLES ---
{example_qa}
---

The execution result of the original source code is below.
--- Original Result ---
{execution_result}

Note that your written code will be added to the lines with substring:
"# OPTIGUIDE *** CODE GOES HERE"
So, you don't need to write other code, such as m.optimize() or m.update().
You just need to write code snippet in ```python ...``` block.

Be mindful that order of the code, because some variables might not be defined
yet when your code is inserted into the source code.
"""

SAFEGUARD_SYSTEM_MSG = """
Given the original source code:
{source_code}

Is the following code safe (not malicious code to break security,
privacy, or hack the system) to run?
Answer only one word.
If not safe, answer `DANGER`; else, answer `SAFE`.
"""

# %% Constant strings to match code lines in the source code.
DATA_CODE_STR = "# OPTIGUIDE DATA CODE GOES HERE"
CONSTRAINT_CODE_STR = "# OPTIGUIDE CONSTRAINT CODE GOES HERE"


# %%
class OptiGuideAgent(AssistantAgent):
    """(Experimental) OptiGuide is an agent to answer
    users questions for supply chain-related coding project.

    The OptiGuide agent manages two assistant agents (writer and safeguard).
    """

    def __init__(
        self,
        name,
        source_code,
        solver_software="gurobi",
        doc_str="",
        example_qa="",
        debug_times=3,
        use_safeguard=True,
        **kwargs,
    ):
        """
        Args:
            name (str): agent name.
            source_code (str): The original source code to run.
            doc_str (str): docstring for helper functions if existed.
            example_qa (str): training examples for in-context learning.
            debug_times (int): number of debug tries we allow for LLM to answer
                each question.
            **kwargs (dict): Please refer to other kwargs in
                [AssistantAgent](assistant_agent#__init__) and
                [ResponsiveAgent](responsive_agent#__init__).
        """
        assert source_code.find(DATA_CODE_STR) >= 0, "DATA_CODE_STR not found."
        assert source_code.find(CONSTRAINT_CODE_STR) >= 0, "CONSTRAINT_CODE_STR not found."

        super().__init__(name, **kwargs)
        self._source_code = source_code
        self._doc_str = doc_str
        self._example_qa = example_qa
        assert solver_software in ["gurobi", "pyomo"], "Unknown solver software."

        self._solver_software = solver_software
        self._origin_execution_result = _run_with_exec(source_code, self._solver_software)
        self._writer = trace(AssistantAgent)("writer", llm_config=self.llm_config)
        self._safeguard = trace(AssistantAgent)("safeguard", llm_config=self.llm_config)
        self._debug_times_left = self.debug_times = debug_times
        self._use_safeguard = use_safeguard
        self._success = False

    # def generate_reply(
    #     self,
    #     messages: Optional[List[Dict]] = None,
    #     default_reply: Optional[Union[str, Dict]] = "",
    #     sender: Optional[Agent] = None,
    # ) -> Union[str, Dict, None]:
    def generate_reply(
        self,
        messages=None,
        sender=None,
        exclude=None,
    ) -> Union[str, Dict, None]:
        # Remove unused variables:
        # The message is already stored in self._oai_messages
        # del messages, default_reply
        del messages

        """Reply based on the conversation history."""
        if sender not in [self._writer, self._safeguard]:
            # Step 1: receive the message from the user
            user_chat_history = "\nHere are the history of discussions:\n" f"{self._oai_messages[sender]}"
            writer_sys_msg = (
                WRITER_SYSTEM_MSG.format(
                    solver_software=self._solver_software,
                    source_code=self._source_code,
                    doc_str=self._doc_str,
                    example_qa=self._example_qa,
                    execution_result=self._origin_execution_result,
                )
                + user_chat_history
            )
            safeguard_sys_msg = SAFEGUARD_SYSTEM_MSG.format(source_code=self._source_code) + user_chat_history
            self._writer.update_system_message(writer_sys_msg)
            self._safeguard.update_system_message(safeguard_sys_msg)
            self._writer.reset()
            self._safeguard.reset()
            self._debug_times_left = self.debug_times
            self._success = False
            # Step 2-6: code, safeguard, and interpret
            self.initiate_chat(self._writer, message=CODE_PROMPT)
            if self._success:
                # step 7: receive interpret result
                reply = self.last_message_node(self._writer)["content"]
            else:
                reply = "Sorry. I cannot answer your question."
            # Finally, step 8: send reply to user
            return reply
        if sender == self._writer:
            # reply to writer
            return self._generate_reply_to_writer(sender)
        # no reply to safeguard

    def _generate_reply_to_writer(self, sender):
        if self._success:
            # no reply to writer
            return

        _, code = extract_code(self.last_message_node(sender)["content"])[0]

        # Step 3: safeguard
        safe_msg = ""
        if self._use_safeguard:
            self.initiate_chat(message=SAFEGUARD_PROMPT.format(code=code), recipient=self._safeguard)
            safe_msg = self.last_message_node(self._safeguard)["content"]
        else:
            safe_msg = "SAFE"

        if safe_msg.find("DANGER") < 0:
            # Step 4 and 5: Run the code and obtain the results
            src_code = _insert_code(self._source_code, code, self._solver_software)
            execution_rst = _run_with_exec(src_code, self._solver_software)
            print(colored(str(execution_rst), "yellow"))
            if type(execution_rst) in [str, int, float]:
                # we successfully run the code and get the result
                self._success = True
                # Step 6: request to interpret results
                return INTERPRETER_PROMPT.format(execution_rst=execution_rst)
        else:
            # DANGER: If not safe, try to debug. Redo coding
            execution_rst = """
Sorry, this new code is not safe to run. I would not allow you to execute it.
Please try to find a new way (coding) to answer the question."""
        if self._debug_times_left > 0:
            # Try to debug and write code again (back to step 2)
            self._debug_times_left -= 1
            return DEBUG_PROMPT.format(error_type=type(execution_rst), error_message=str(execution_rst))


# %% Helper functions to edit and run code.
# Here, we use a simplified approach to run the code snippet, which would
# replace substrings in the source code to get an updated version of code.
# Then, we use exec to run the code snippet.
# This approach replicate the evaluation section of the OptiGuide paper.


def _run_with_exec(src_code: str, solver_software: str) -> Union[str, Exception]:
    """Run the code snippet with exec.

    Args:
        src_code (str): The source code to run.

    Returns:
        object: The result of the code snippet.
            If the code succeed, returns the objective value (float or string).
            else, return the error (exception)
    """
    locals_dict = {}
    locals_dict.update(globals())
    locals_dict.update(locals())

    timeout = Timeout(60, TimeoutError("This is a timeout exception, in case " "GPT's code falls into infinite loop."))
    try:
        exec(src_code, locals_dict, locals_dict)
    except Exception as e:
        return e
    finally:
        timeout.cancel()

    try:
        ans = _get_optimization_result(locals_dict, solver_software)
    except Exception as e:
        return e

    return ans


def _replace(src_code: str, old_code: str, new_code: str) -> str:
    """
    Inserts new code into the source code by replacing a specified old
    code block.

    Args:
        src_code (str): The source code to modify.
        old_code (str): The code block to be replaced.
        new_code (str): The new code block to insert.

    Returns:
        str: The modified source code with the new code inserted.

    Raises:
        None

    Example:
        src_code = 'def hello_world():\n    print("Hello, world!")\n\n# Some
        other code here'
        old_code = 'print("Hello, world!")'
        new_code = 'print("Bonjour, monde!")\nprint("Hola, mundo!")'
        modified_code = _replace(src_code, old_code, new_code)
        print(modified_code)
        # Output:
        # def hello_world():
        #     print("Bonjour, monde!")
        #     print("Hola, mundo!")
        # Some other code here
    """
    pattern = r"( *){old_code}".format(old_code=old_code)
    head_spaces = re.search(pattern, src_code, flags=re.DOTALL).group(1)
    new_code = "\n".join([head_spaces + line for line in new_code.split("\n")])
    rst = re.sub(pattern, new_code, src_code)
    return rst


def _insert_code(src_code: str, new_lines: str, solver_software: str) -> str:
    """insert a code patch into the source code.


    Args:
        src_code (str): the full source code
        new_lines (str): The new code.

    Returns:
        str: the full source code after insertion (replacement).
    """
    if solver_software == "gurobi":
        if new_lines.find("addConstr") >= 0:
            return _replace(src_code, CONSTRAINT_CODE_STR, new_lines)
    elif solver_software == "pyomo":
        if new_lines.find("Constraint") >= 0:
            return _replace(src_code, CONSTRAINT_CODE_STR, new_lines)

    return _replace(src_code, DATA_CODE_STR, new_lines)


def _get_optimization_result(locals_dict: dict, solver_software: str) -> str:
    if solver_software == "gurobi":
        status = locals_dict["m"].Status
        if status != GRB.OPTIMAL:
            if status == GRB.UNBOUNDED:
                ans = "unbounded"
            elif status == GRB.INF_OR_UNBD:
                ans = "inf_or_unbound"
            elif status == GRB.INFEASIBLE:
                ans = "infeasible"
                m = locals_dict["m"]
                m.computeIIS()
                constrs = [c.ConstrName for c in m.getConstrs() if c.IISConstr]
                ans += "\nConflicting Constraints:\n" + str(constrs)
            else:
                ans = "Model Status:" + str(status)
        else:
            ans = "Optimization problem solved. The objective value is: " + str(locals_dict["m"].objVal)
    elif solver_software == "pyomo":
        status = locals_dict["m"].solver.termination_condition
        if status != TerminationCondition.optimal:
            if status == TerminationCondition.unbounded:
                ans = "unbounded"
            elif status == TerminationCondition.infeasibleOrUnbounded:
                ans = "inf_or_unbound"
            elif status == TerminationCondition.infeasible:
                ans = "infeasible"
            else:
                ans = "Model Status:" + str(status)
        else:
            ans = "Optimization problem solved. The objective value is: " + str(locals_dict["model"].obj())
    else:
        raise ValueError("Unknown solver software: " + solver_software)

    return ans


# %% Prompt for OptiGuide
CODE_PROMPT = """
Answer Code:
"""

DEBUG_PROMPT = """

While running the code you suggested, I encountered the {error_type}:
--- ERROR MESSAGE ---
{error_message}

Please try to resolve this bug, and rewrite the code snippet.
--- NEW CODE ---
"""

SAFEGUARD_PROMPT = """
--- Code ---
{code}

--- One-Word Answer: SAFE or DANGER ---
"""

INTERPRETER_PROMPT = """Here are the execution results: {execution_rst}

Can you organize these information to a human readable answer?
Remember to compare the new results to the original results you obtained in the
beginning.

--- HUMAN READABLE ANSWER ---
"""

# ============= Trace / Optimization Code ===============

import autogen
import requests
from autogen.trace import trace

if __name__ == "__main__":
    config_list = autogen.config_list_from_json(
        "OAI_CONFIG_LIST",
        filter_dict={
            "model": {
                "gpt-4",
                "gpt4",
                "gpt-4-32k",
                "gpt-4-32k-0314",
                "gpt-3.5-turbo",
                "gpt-3.5-turbo-16k",
                "gpt-3.5-turbo-0301",
                "chatgpt-35-turbo-0301",
                "gpt-35-turbo-v0301",
            }
        },
    )

    # Get the source code of our coffee example
    code_url = "https://raw.githubusercontent.com/microsoft/OptiGuide/main/benchmark/application/coffee.py"
    response = requests.get(code_url)

    # Check if the request was successful
    if response.status_code == 200:
        # Get the text content from the response
        code = response.text
    else:
        raise RuntimeError("Failed to retrieve the file.")

    # code = open(code_url, "r").read() # for local files

    # show the first head and tail of the source code
    print("\n".join(code.split("\n")[:10]))
    print(".\n" * 3)
    print("\n".join(code.split("\n")[-10:]))

    # In-context learning examples.
    example_qa = """
    ----------
    Question: Why is it not recommended to use just one supplier for roastery 2?
    Answer Code:
    ```python
    z = m.addVars(suppliers, vtype=GRB.BINARY, name="z")
    m.addConstr(sum(z[s] for s in suppliers) <= 1, "_")
    for s in suppliers:
        m.addConstr(x[s,'roastery2'] <= capacity_in_supplier[s] * z[s], "_")
    ```

    ----------
    Question: What if there's a 13% jump in the demand for light coffee at cafe1?
    Answer Code:
    ```python
    light_coffee_needed_for_cafe["cafe1"] = light_coffee_needed_for_cafe["cafe1"] * (1 + 13/100)
    ```
    """

    agent = trace(OptiGuideAgent)(
        name="OptiGuide Coffee Example",
        source_code=code,
        debug_times=1,
        example_qa="",
        llm_config={
            "seed": 42,
            "config_list": config_list,
        },
    )

    user = trace(autogen.UserProxyAgent)(
        "user", max_consecutive_auto_reply=0, human_input_mode="NEVER", code_execution_config=False
    )

    user.initiate_chat(agent, message="What if we prohibit shipping from supplier 1 to roastery 2?")

    # ValueError: dictionary update sequence element #0 has length 1; 2 is required
