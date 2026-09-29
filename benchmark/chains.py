"""Depth chains for the benchmark: 3 datasets, 5-6 causally dependent rounds
each, replacing the earlier breadth design (5 tables x 3 independent questions).

Why chains: reuse payoff is proportional to the cost of rebuilding. A
one-shot question on a small CSV costs one read_csv to redo, so a store can
never win and the benchmark has no headroom. Each round here consumes
something the previous round *produced* — a column that only exists after
R1's derivation, a segment defined by R2's metric — so the chain cannot be
answered without redoing the work. Reuse becomes load-bearing rather than
optional.

The file arm faces exactly the same chain on disk: R1's outputs sit in its
workspace and it may find and reuse them. That is the fair strongest file
baseline; the question measured is which is cheaper, filesystem
archaeology or a searchable store.

Every gold answer is computed here with pandas — no LLM judge. Each round's
question fully determines its computation (explicit filters, explicit
round/decimal places) so the answer is unambiguous.
"""

from __future__ import annotations

# Each round: question text (what the agent is asked), the tagged answer
# format, and a reference computation producing the gold values.
# Tag names follow DABench's @tag[value] convention so score.py is unchanged.

CHAINS = [
    {
        "table": "diamonds.csv",
        "title": "Diamonds: clean -> derive -> segment -> compare",
        "rounds": [
            {
                "round": 1,
                "stage": "build",
                "depends_on": [],
                "question": (
                    "Load diamonds.csv. Some rows are physically impossible: they have a "
                    "length, width or depth (x, y or z) of 0. Drop every such row, and in the "
                    "remaining data add a column price_per_carat = price / carat. "
                    "How many rows remain, and what is the mean price_per_carat over all of them?"
                ),
                "format": (
                    "@n_rows[n] @mean_price_per_carat[value] where n is the surviving row count "
                    "and value is the mean rounded to two decimals."
                ),
                "reference": "diamonds_r1",
            },
            {
                "round": 2,
                "stage": "derive",
                "depends_on": [1],
                "question": (
                    "Using the cleaned data with the price_per_carat column from before, report "
                    "the median price and the number of diamonds for each cut, from Fair to "
                    "Premium cut."
                ),
                "format": (
                    "@median_price[cut:value] @count[cut:n] for each of the five cut levels, "
                    "values rounded to two decimals."
                ),
                "reference": "diamonds_r2",
            },
            {
                "round": 3,
                "stage": "narrow",
                "depends_on": [1],
                "question": (
                    "Using the same cleaned diamonds data, restrict to Ideal cut only, and "
                    "report the median price and mean price_per_carat within that cut for each "
                    "clarity level (I1, SI2, SI1, VS2, VS1, VVS2, VVS1), in that order."
                ),
                "format": (
                    "@median_price[clarity:value] @mean_ppc[clarity:value] for each clarity "
                    "level listed, rounded to two decimals."
                ),
                "reference": "diamonds_r3",
            },
            {
                "round": 4,
                "stage": "segment",
                "depends_on": [2],
                "question": (
                    "Define a 'premium' diamond as one in the cleaned data whose price_per_carat "
                    "is above the overall mean price_per_carat. What share of the surviving rows "
                    "does that represent, as a percentage to two decimals? Among premium diamonds "
                    "only, which cut has the highest count, and what is that count?"
                ),
                "format": (
                    "@premium_pct[pct] @top_premium_cut[cut] @top_premium_count[n] where pct is a "
                    "percentage rounded to two decimals."
                ),
                "reference": "diamonds_r4",
            },
            {
                "round": 5,
                "stage": "compare",
                "depends_on": [3, 4],
                "question": (
                    "Take the premium diamonds from the previous step. Within the Ideal-cut "
                    "premium subset, what is the median carat, and how many diamonds are in it? "
                    "Also report the median price_per_carat of that same subset."
                ),
                "format": (
                    "@median_carat[value] @subset_size[n] @median_ppc[value] rounded to two "
                    "decimals except the count."
                ),
                "reference": "diamonds_r5",
            },
            {
                "round": 6,
                "stage": "synthesise",
                "depends_on": [1, 4],
                "question": (
                    "Across the whole cleaned dataset, compute the Pearson correlation between "
                    "carat and price_per_carat. Separately, within premium diamonds only, "
                    "compute the same correlation. Report both to four decimal places."
                ),
                "format": (
                    "@corr_all[value] @corr_premium[value] each rounded to four decimals."
                ),
                "reference": "diamonds_r6",
            },
        ],
    },
    {
        "table": "vgsales.csv",
        "title": "Video games: totals -> shares -> era -> platform -> concentration",
        "rounds": [
            {
                "round": 1,
                "stage": "build",
                "depends_on": [],
                "question": (
                    "Load vgsales.csv. Some rows have a missing Publisher. Add a column "
                    "total_earlier = NA_Sales + EU_Sales + JP_Sales + Other_Sales. Report the "
                    "total Global_Sales summed over all rows, and the number of rows whose "
                    "Publisher is missing."
                ),
                "format": (
                    "@total_global[value] @missing_publisher[n] where value is rounded to two "
                    "decimals and n is a count."
                ),
                "reference": "vgsales_r1",
            },
            {
                "round": 2,
                "stage": "derive",
                "depends_on": [1],
                "question": (
                    "Using total_earlier, compute total Global_Sales per Genre, and each genre's "
                    "share of the overall total as a percentage. Report the top three genres by "
                    "that share, highest first."
                ),
                "format": (
                    "@genre_share[genre:pct] for the top three genres, pct rounded to two decimals."
                ),
                "reference": "vgsales_r2",
            },
            {
                "round": 3,
                "stage": "era",
                "depends_on": [2],
                "question": (
                    "Restrict to the top genre from the previous step. Within it, compute total "
                    "Global_Sales per release decade (floor(Year/10)*10), and report the decade "
                    "with the highest total along with that total."
                ),
                "format": (
                    "@top_decade[decade] @decade_sales[value] where decade is a four-digit year "
                    "like 1990 and value is rounded to two decimals."
                ),
                "reference": "vgsales_r3",
            },
            {
                "round": 4,
                "stage": "narrow",
                "depends_on": [3],
                "question": (
                    "Within the top genre and only the top decade found previously, which "
                    "Platform has the highest total Global_Sales, and what is that total? How "
                    "many distinct titles does that platform have in that slice?"
                ),
                "format": (
                    "@top_platform[platform] @platform_sales[value] @platform_titles[n] with "
                    "value rounded to two decimals."
                ),
                "reference": "vgsales_r4",
            },
            {
                "round": 5,
                "stage": "concentration",
                "depends_on": [2, 4],
                "question": (
                    "Within the top genre, compute total Global_Sales per Publisher. What "
                    "percentage of that genre's total comes from its single largest publisher, "
                    "and what is that publisher's name?"
                ),
                "format": (
                    "@top_publisher[publisher] @publisher_pct[pct] with pct rounded to two "
                    "decimals."
                ),
                "reference": "vgsales_r5",
            },
            {
                "round": 6,
                "stage": "synthesise",
                "depends_on": [3, 5],
                "question": (
                    "Compare two ratios on the top genre: the share of that genre's Global_Sales "
                    "coming from its top decade, and the share coming from its top publisher. "
                    "Report both percentages to two decimals, and state which is larger."
                ),
                "format": (
                    "@decade_share[pct] @publisher_share[pct] @larger[which] where which is "
                    "either 'decade' or 'publisher'."
                ),
                "reference": "vgsales_r6",
            },
        ],
    },
    {
        "table": "census.csv",
        "title": "Census: missing values -> derived capital -> segments -> comparison",
        "rounds": [
            {
                "round": 1,
                "stage": "build",
                "depends_on": [],
                "question": (
                    "Load census.csv. In this dataset a missing value is recorded as the literal "
                    "string '?'. Count how many rows have that marker in workclass, and how many "
                    "in occupation. Add a column capital_net = capital-gain - capital-loos. "
                    "Report both missing counts and the total capital_net summed over all rows."
                ),
                "format": (
                    "@missing_workclass[n] @missing_occupation[n] @total_capital_net[value] with "
                    "value rounded to two decimals."
                ),
                "reference": "census_r1",
            },
            {
                "round": 2,
                "stage": "derive",
                "depends_on": [1],
                "question": (
                    "Using capital_net, how many rows have capital_net greater than 0, and what "
                    "is the mean education-num (education-num) among those rows? Report the count "
                    "and the mean rounded to four decimals."
                ),
                "format": (
                    "@n_capital_positive[n] @mean_education[value] with value rounded to four "
                    "decimals."
                ),
                "reference": "census_r2",
            },
            {
                "round": 3,
                "stage": "narrow",
                "depends_on": [2],
                "question": (
                    "Restrict to rows with capital_net greater than 0. Within that subset, group "
                    "by sex (trim any leading or trailing whitespace) and report the mean "
                    "hours-per-week and the mean education-num for each sex present."
                ),
                "format": (
                    "@mean_hours[sex:value] @mean_education[sex:value] for each sex, values "
                    "rounded to two decimals."
                ),
                "reference": "census_r3",
            },
            {
                "round": 4,
                "stage": "segment",
                "depends_on": [1],
                "question": (
                    "Define a 'long hours' row as one working more than 40 hours per week. What "
                    "percentage of all rows is that, to two decimals? Among long-hours rows, "
                    "which marital-status has the highest count, and what is that count?"
                ),
                "format": (
                    "@long_hours_pct[pct] @top_marital[status] @top_marital_count[n]."
                ),
                "reference": "census_r4",
            },
            {
                "round": 5,
                "stage": "compare",
                "depends_on": [3, 4],
                "question": (
                    "Take the long-hours subset from the previous step, and within it take only "
                    "rows with capital_net greater than 0. How many rows remain, and what is the "
                    "mean capital_net of that doubly-filtered subset?"
                ),
                "format": (
                    "@n_remaining[n] @mean_capital_net[value] with value rounded to two decimals."
                ),
                "reference": "census_r5",
            },
            {
                "round": 6,
                "stage": "synthesise",
                "depends_on": [4, 5],
                "question": (
                    "Across all rows, compute the Pearson correlation between hours-per-week and "
                    "education-num, to four decimals. Then compute the same correlation within "
                    "long-hours rows only, to four decimals."
                ),
                "format": (
                    "@corr_all[value] @corr_long_hours[value] each rounded to four decimals."
                ),
                "reference": "census_r6",
            },
        ],
    },
]
