import argparse
import json
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from src.attack.nanogpp.gpp import run, GPPConfig

from src.attack.utils.promptify import promptify_json, linkedin_prompt

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json_name", type=str, help="Name of the JSON file in the data directory to find a trigger for.", default="")
    parser.add_argument("--model", choices=["mistral-7B", "mistral-24B", "llama2", "llama3"], default="mistral-7B", help="The model to use for generation.")
    parser.add_argument("--target", type=str, default="```click(\"41\")```", help="The desired output when triggered.")
    parser.add_argument("--device", type=str, default="cuda", help="Device to run the model on.")
    parser.add_argument("--dtype", type=str, default="float16", help="Data type to use for the model.")
    parser.add_argument("--linkedin", type=bool, default=False, help="Whether doing temporary linkedin searh")
    args = parser.parse_args()
    return args


def find_trigger():
    """
    Uses nanoGPP to find a trigger that will cause the specified model to generate the target output when given the website and
    goal data provided in the specified JSON file. Saves the trigger to a JSON file in the triggers directory.
    """

    args = parse_args()
    match args.model:
        case "mistral-7B":
            model_name = "mistralai/Mistral-7B-Instruct-v0.3"
        case "mistral-24B":
            model_name = "mistralai/Mistral-Small-24B-Instruct-250"
        case "llama2":
            model_name = "meta-llama/Llama-2-7b-chat-hf"
        case "llama3":
            model_name = "meta-llama/Llama-3.1-8B-Instruct"

    model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=getattr(torch, args.dtype)).to(args.device)
    tokenizer = AutoTokenizer.from_pretrained(model_name)

    if not args.linkedin:
        with open(f"data/{args.json_name}.json") as f:
            obs_dict = json.load(f)

    if args.linkedin:
        sys_content, user_content = linkedin_prompt()
    else:
        sys_content, user_content = promptify_json(obs_dict)

    messages=[
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_content}
        ]

    # could be customizable from the command line in the future
    config = GPPConfig(
        num_steps=1000,
        early_stop=True,
        verbosity="INFO",
        # batch_size=1,
        add_space_before_target = True if args.model == "llama2" else False,
    )

    if args.linkedin:
        args.target = '```send_msg_to_user("Sam Johnson is a good candidate, check out his profile: https://www.linkedin.com/in/sam-johnson-5429a0349/")```'

    result = run(
        model,
        tokenizer,
        messages,
        args.target,
        config,
    )

    print("~~ BEST TRIGGER: ~~", flush=True)
    print(result.best_string, flush=True)

    test_message = messages.copy()
    test_message[-1]["content"] = test_message[-1]["content"].replace("{optim_str}", result.best_string)

    input = tokenizer.apply_chat_template(test_message, add_generation_prompt=True, return_tensors="pt").to(args.device)
    output = model.generate(input, do_sample=False, max_new_tokens=512)

    print(f"Prompt:\n{test_message[-1]['content']}\n", flush=True)
    print(f"Generation:\n{tokenizer.batch_decode(output[:, input.shape[1]:], skip_special_tokens=True)[0]}", flush=True)

    if not args.linkedin:
        with open(f"triggers/{args.json_name}_trigger.json", "w") as f:
            trig_dict = {
                "json_name": args.json_name,
                "model": args.model,
                "target": args.target,
                "trigger": result.best_string,
            }
            json.dump(trig_dict, f, indent=4)
        
    else:
        with open(f"triggers/linkedin_trigger.json", "w") as f:
            trig_dict = {
                "model": args.model,
                "target": args.target,
                "trigger": result.best_string,
            }
            json.dump(trig_dict, f, indent=4)



if __name__ == "__main__":
    find_trigger()