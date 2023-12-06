import logging
import re
from typing import Optional

from theflow import Param

from kotaemon.agents.base import BaseAgent, BaseLLM
from kotaemon.agents.io import AgentAction, AgentFinish, AgentOutput, AgentType
from kotaemon.agents.tools import BaseTool
from kotaemon.llms import PromptTemplate

FINAL_ANSWER_ACTION = "Final Answer:"


class ReactAgent(BaseAgent):
    """
    Sequential ReactAgent class inherited from BaseAgent.
    Implementing ReAct agent paradigm https://arxiv.org/pdf/2210.03629.pdf
    """

    name: str = "ReactAgent"
    agent_type: AgentType = AgentType.react
    description: str = "ReactAgent for answering multi-step reasoning questions"
    llm: BaseLLM
    prompt_template: Optional[PromptTemplate] = None
    plugins: list[BaseTool] = Param(
        default_callback=lambda _: [], help="List of tools to be used in the agent. "
    )
    examples: dict[str, str | list[str]] = Param(
        default_callback=lambda _: {}, help="Examples to be used in the agent. "
    )
    intermediate_steps: list[tuple[AgentAction | AgentFinish, str]] = Param(
        default_callback=lambda _: [],
        help="List of AgentAction and observation (tool) output",
    )
    max_iterations: int = 10
    strict_decode: bool = False

    def _compose_plugin_description(self) -> str:
        """
        Compose the worker prompt from the workers.

        Example:
        toolname1[input]: tool1 description
        toolname2[input]: tool2 description
        """
        prompt = ""
        try:
            for plugin in self.plugins:
                prompt += f"{plugin.name}[input]: {plugin.description}\n"
        except Exception:
            raise ValueError("Worker must have a name and description.")
        return prompt

    def _construct_scratchpad(
        self, intermediate_steps: list[tuple[AgentAction | AgentFinish, str]] = []
    ) -> str:
        """Construct the scratchpad that lets the agent continue its thought process."""
        thoughts = ""
        for action, observation in intermediate_steps:
            thoughts += action.log
            thoughts += f"\nObservation: {observation}\nThought:"
        return thoughts

    def _parse_output(self, text: str) -> Optional[AgentAction | AgentFinish]:
        """
        Parse text output from LLM for the next Action or Final Answer
        Using Regex to parse "Action:\n Action Input:\n" for the next Action
        Using FINAL_ANSWER_ACTION to parse Final Answer

        Args:
            text[str]: input text to parse
        """
        includes_answer = FINAL_ANSWER_ACTION in text
        regex = (
            r"Action\s*\d*\s*:[\s]*(.*?)[\s]*Action\s*\d*\s*Input\s*\d*\s*:[\s]*(.*)"
        )
        action_match = re.search(regex, text, re.DOTALL)
        action_output: Optional[AgentAction | AgentFinish] = None
        if action_match:
            if includes_answer:
                raise Exception(
                    "Parsing LLM output produced both a final answer "
                    f"and a parse-able action: {text}"
                )
            action = action_match.group(1).strip()
            action_input = action_match.group(2)
            tool_input = action_input.strip(" ")
            # ensure if its a well formed SQL query we don't remove any trailing " chars
            if tool_input.startswith("SELECT ") is False:
                tool_input = tool_input.strip('"')

            action_output = AgentAction(action, tool_input, text)

        elif includes_answer:
            action_output = AgentFinish(
                {"output": text.split(FINAL_ANSWER_ACTION)[-1].strip()}, text
            )
        else:
            if self.strict_decode:
                raise Exception(f"Could not parse LLM output: `{text}`")
            else:
                action_output = AgentFinish({"output": text}, text)

        return action_output

    def _compose_prompt(self, instruction) -> str:
        """
        Compose the prompt from template, worker description, examples and instruction.
        """
        agent_scratchpad = self._construct_scratchpad(self.intermediate_steps)
        tool_description = self._compose_plugin_description()
        tool_names = ", ".join([plugin.name for plugin in self.plugins])
        if self.prompt_template is None:
            from .prompt import zero_shot_react_prompt

            self.prompt_template = zero_shot_react_prompt
        return self.prompt_template.populate(
            instruction=instruction,
            agent_scratchpad=agent_scratchpad,
            tool_description=tool_description,
            tool_names=tool_names,
        )

    def _format_function_map(self) -> dict[str, BaseTool]:
        """Format the function map for the open AI function API.

        Return:
            Dict[str, Callable]: The function map.
        """
        # Map the function name to the real function object.
        function_map = {}
        for plugin in self.plugins:
            function_map[plugin.name] = plugin
        return function_map

    def clear(self):
        """
        Clear and reset the agent.
        """
        self.intermediate_steps = []

    def run(self, instruction, max_iterations=None) -> AgentOutput:
        """
        Run the agent with the given instruction.

        Args:
            instruction: Instruction to run the agent with.
            max_iterations: Maximum number of iterations
                of reasoning steps, defaults to 10.

        Return:
            AgentOutput object.
        """
        if not max_iterations:
            max_iterations = self.max_iterations
        assert max_iterations > 0

        self.clear()
        logging.info(f"Running {self.name} with instruction: {instruction}")
        total_cost = 0.0
        total_token = 0
        status = "failed"
        response_text = None

        for step_count in range(1, max_iterations + 1):
            prompt = self._compose_prompt(instruction)
            logging.info(f"Prompt: {prompt}")
            response = self.llm(
                prompt, stop=["Observation:"]
            )  # could cause bugs if llm doesn't have `stop` as a parameter
            response_text = response.text
            logging.info(f"Response: {response_text}")
            action_step = self._parse_output(response_text)
            if action_step is None:
                raise ValueError("Invalid action")
            is_finished_chain = isinstance(action_step, AgentFinish)
            if is_finished_chain:
                result = ""
            else:
                assert isinstance(action_step, AgentAction)
                action_name = action_step.tool
                tool_input = action_step.tool_input
                logging.info(f"Action: {action_name}")
                logging.info(f"Tool Input: {tool_input}")
                result = self._format_function_map()[action_name](tool_input)
                logging.info(f"Result: {result}")

            self.intermediate_steps.append((action_step, result))
            if is_finished_chain:
                logging.info(f"Finished after {step_count} steps.")
                status = "finished"
                break
        else:
            status = "stopped"

        return AgentOutput(
            text=response_text,
            agent_type=self.agent_type,
            status=status,
            total_tokens=total_token,
            total_cost=total_cost,
            intermediate_steps=self.intermediate_steps,
            max_iterations=max_iterations,
        )
