# FraudShield User Manual

This guide is for people who use the FraudShield web page. You do not need any technical
background. If something on the page does not work and the steps in this guide do not
help, contact the person who runs the system; their guide is
[operator_guide.md](operator_guide.md).

## What FraudShield does

FraudShield looks at one payment at a time and gives it a **fraud score**. A high score
means the payment looks similar to payments that were fraud in the practice data the
system learned from. If the score is high enough, the page says the payment is
**flagged for review**, so that a person can take a closer look.

Please keep three things in mind:

- FraudShield is a college demonstration built on invented (synthetic) payments. It does
  not approve or block real payments.
- A flag is a hint for a person to check, not proof of fraud.
- "Not flagged" does not guarantee that a payment is legitimate.

## Opening the web page

Ask the operator for the address of the transaction page. On the machine where the system
runs it is `http://127.0.0.1:8080`. Open it in your web browser. At the top of the page
a line shows the connection. When everything is working it says **API ready**, followed by
the model name and version.

[Screenshot to add: the page just after opening, with "API ready" shown]

If it says "API unavailable" or "API is not ready", see "What to do if something does not
work" below.

## Checking a payment

The box **Review a transaction** has three fields:

| Field | What to enter |
|---|---|
| **Transaction type** | Choose one of the five types the page offers: TRANSFER, CASH_OUT, PAYMENT, CASH_IN or DEBIT. These are the types used in the practice data. (Short meanings such as "money sent to another account" for TRANSFER are everyday descriptions, not defined in this project.) |
| **Transaction amount** | How much money the payment is for. Use a number that is zero or more, for example `100000`. |
| **Sender balance before payment** | How much money the sender had before the payment, in the same currency unit as the amount. Zero or more. |

Amount and balance must use the same unit. The values already on the page are an invented
example payment; change them freely. The page does not save what you type.

Then choose one of two buttons:

- **Check transaction** gives the verdict and the score.
- **Check + explain** gives the same and also shows why the score came out as it did.

[Screenshot to add: the form filled in, with both buttons visible]

If a field is empty, negative or not a number, a short red message appears directly under
that field, for example "The sender balance cannot be negative." Fix the field and the
message disappears. Nothing is sent until all fields are valid.

## Reading the result

The box **Model assessment** shows the result.

- **Flagged for review** (in red) or **Not flagged by the model** (in green). The words
  carry the meaning, so the result is clear even if you cannot tell the colours apart.
- **Fraud score** (also labelled "model score"): a number between 0 and 1. A higher number
  means the payment looks more like the fraud examples. It is not a percentage chance of
  fraud; the page itself says "The score is not a calibrated fraud probability."
- **Decision threshold** (labelled "flagged at or above"): the line the score is compared
  with. A payment is flagged when its fraud score is equal to or higher than this number.
- Below them, the model name and version that produced the result.

[Screenshot to add: a result showing "Flagged for review" with the score and threshold]

### Why this score (after "Check + explain")

The section **Why this score?** (also labelled "feature contributions") is a table of
**factors**: the facts about your payment that the model looks at, such as the size of the
amount or the type of transaction. Next to each factor, **Effect on score** shows a signed
number:

- A **plus** number (with "+") pushed the score **towards fraud**.
- A **minus** number pushed it **away** from fraud.
- The bigger the number, ignoring the sign, the bigger the push. The biggest are listed
  first.

These numbers describe how the model reached its score. They do not prove why a payment is
fraud and they do not show cause and effect in the real world. A factor can have an effect
even when it is "off", for example a transaction type that does not apply.

The small line under the table (it begins "Base ...") is a technical check showing that the
pushes add up to the model's raw result. You can ignore it.

| Factor on the page | In plain words |
|---|---|
| Log transaction amount | The size of the amount (scaled so that very large amounts are comparable) |
| Log sender balance | The size of the sender's balance (scaled the same way) |
| Amount relative to amount + balance | The amount as a share of amount plus balance |
| Amount exceeds balance indicator | Whether the amount is larger than the balance |
| TRANSFER / CASH_OUT / PAYMENT / CASH_IN / DEBIT indicator | Whether the payment is of that type |

[Screenshot to add: the "Why this score?" table with plus and minus values]

## Every message the page can show

| Message on the page | What it means | What to do |
|---|---|---|
| Checking API… / Checking API · (address) | The page is asking the system whether it is ready | Wait a moment |
| API ready · (model) · version (n) · (address) | The system is running and a model is loaded | Nothing; you can use the page |
| API is not ready · (address) | The system answered but has not finished starting | Wait a minute and reload; if it stays, contact the operator |
| API unavailable · (address) — check the address and backend | The page cannot reach the system | Check the address (see below); if it is right, contact the operator |
| API responding · (model) · version (n) | A check has just succeeded | Nothing |
| Submit a transaction to view its assessment. | No result yet | Fill the form and choose a button |
| Checking… | The verdict is being calculated | Wait |
| Calculating explanation… | The explanation is being calculated; it takes a little longer | Wait |
| Flagged for review | The score is at or above the threshold | Have a person look at the payment |
| Not flagged by the model | The score is below the threshold | No action; this is not a guarantee |
| Enter the transaction amount. / Enter the sender balance. | A field is empty | Type a number |
| Enter the (field) as a number, for example 100000. | The field contains something that is not a number | Type digits only |
| The (field) must be a finite number. | The number is not usable | Type an ordinary number |
| The (field) cannot be negative. | A negative number was typed | Type zero or more |
| Enter finite numerical amounts. | The amount or balance is not a usable number | Correct both numbers |
| Invalid input. Check the type, amount, and balance. | The system rejected the values | Check all three fields |
| Request timed out. Check the backend and try again. | The system took too long to answer | Try again; if it repeats, contact the operator |
| Cannot reach the API. Check the backend and browser connection. | The page could not connect at all | Check the address; contact the operator |
| API request failed (HTTP number). | The system reported a problem | Try again; if it repeats, tell the operator the number |
| API address applied and saved in this browser. | The new connection address was accepted | Nothing |
| API address applied for this page; browser storage is unavailable. | The address works now but will not be remembered | Nothing, or repeat it next time |
| Enter a complete local API address, such as http://127.0.0.1:8000. | The address you typed is not a complete web address | Type it in full, starting with http:// |
| Use an HTTP or HTTPS localhost address with an optional port, without credentials, a path, query or fragment. | The address is not allowed | Use only the form shown, for example http://127.0.0.1:8000 |

## Changing the connection address

At the top of the page, the box **API connection** has a field **Local API address**. Normally
you never need to change it. If the operator gives you a different address:

1. Type it into the field in the form `http://127.0.0.1:8000` (the number after the colon may
   differ).
2. Choose **Apply and check connection**. The page remembers the address in your browser.
3. Choose **Restore default** to go back to `http://127.0.0.1:8000`.

Only addresses on the same machine (`localhost` or `127.0.0.1`) are accepted, without a user
name, password or extra path. The change applies only to the page, not to the browser's own
address bar, which stays on the page's address.

[Screenshot to add: the API connection box]

## What to do if something does not work

1. Reload the page and wait for **API ready**.
2. Check that the address in **Local API address** is the one the operator gave you; choose
   **Restore default** if you are unsure.
3. Try a simple payment such as the example already on the page.
4. If the problem remains, contact the operator and tell them the exact message shown on the
   page. They can use [operator_guide.md](operator_guide.md) to check the system.

## Frequently asked questions

**Is a flagged payment fraud?** Not necessarily. Flagged means "worth a closer look".

**If a payment is not flagged, is it safe?** Not guaranteed. The model can be wrong, and it only
looks at the type, the amount and the balance.

**Does the page store my entries?** No. Only the connection address is remembered in your
browser. The page itself says "Inputs are not saved by this page."

**Why does the model look at so few details?** It only receives the three fields on the page.
Other information, such as names or account numbers, is deliberately not used.

**Can I use real customer data?** No. The system was built and checked on synthetic data and is
a demonstration only.

**Which model gave the result?** The model name and version are shown under the result.

## Glossary

| Term | Meaning |
|---|---|
| Fraud score | A number from 0 to 1 from the model; higher means more similar to fraud examples |
| Decision threshold | The score at or above which a payment is flagged |
| Flagged for review | The score reached the threshold, so a person should check the payment |
| Factor (feature) | A fact about the payment that the model uses, such as the amount |
| Effect on score (contribution) | How much a factor pushed the score up (plus) or down (minus) |
| Log-odds / SHAP | The technical unit and method behind the effects; you can ignore them |
| Model | The program that calculates the score from past examples |
| Synthetic data | Invented example payments, not real customers |
| API | The part of the system the web page talks to; "API unavailable" means that part cannot be reached |
| Operator | The person who runs and looks after the system |
