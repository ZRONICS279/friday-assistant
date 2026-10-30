import json

import httpx
import pytest

from house_agent import recipes
from house_agent.recipes import extract_recipe, parse_duration_minutes, pick_best, recipe_links, search_recipes, summarize

PAGE = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"WebPage","name":"Fluffy Pancakes"},
 {"@type":"Recipe","name":"Fluffy Buttermilk Pancakes",
  "description":"The <b>fluffiest</b> pancakes &amp; they are easy.",
  "author":{"@type":"Person","name":"Ana"},
  "prepTime":"PT10M","cookTime":"PT20M",
  "recipeYield":["8","8 pancakes"],
  "recipeCategory":["Breakfast"],"recipeCuisine":"American",
  "aggregateRating":{"ratingValue":"4.8","ratingCount":"1234"},
  "nutrition":{"@type":"NutritionInformation","calories":"210 kcal","proteinContent":"6 g"},
  "recipeIngredient":["2 cups flour","2 cups buttermilk","2 eggs"],
  "recipeInstructions":[
    {"@type":"HowToSection","name":"Batter","itemListElement":[
      {"@type":"HowToStep","text":"Whisk the dry ingredients."},
      {"@type":"HowToStep","text":"Stir in buttermilk and eggs."}]},
    {"@type":"HowToStep","text":"Cook on a hot griddle."}]}
]}
</script></head><body>story story story</body></html>"""


def test_duration_parsing() -> None:
    assert parse_duration_minutes("PT1H30M") == 90
    assert parse_duration_minutes("P0DT0H45M") == 45
    assert parse_duration_minutes("PT45S") == 1
    assert parse_duration_minutes("PT0M") is None
    assert parse_duration_minutes("30 minutes") is None
    assert parse_duration_minutes(None) is None


def test_extracts_recipe_from_graph() -> None:
    recipe = extract_recipe(PAGE, "https://example.com/pancakes")
    assert recipe["title"] == "Fluffy Buttermilk Pancakes"
    assert recipe["url"] == "https://example.com/pancakes"
    assert recipe["description"] == "The fluffiest pancakes & they are easy."
    assert recipe["author"] == "Ana"
    assert recipe["servings"] == "8"
    assert (recipe["prep_minutes"], recipe["cook_minutes"], recipe["total_minutes"]) == (10, 20, 30)
    assert recipe["rating"] == 4.8 and recipe["rating_count"] == 1234
    assert recipe["category"] == "Breakfast" and recipe["cuisine"] == "American"
    assert recipe["calories"] == "210 kcal" and recipe["protein"] == "6 g"
    assert recipe["ingredients"] == ["2 cups flour", "2 cups buttermilk", "2 eggs"]
    assert recipe["steps"] == [
        "Whisk the dry ingredients.",
        "Stir in buttermilk and eggs.",
        "Cook on a hot griddle.",
    ]
    summary = summarize(recipe)
    assert "steps" not in summary and summary["step_count"] == 3


def test_instructions_as_one_string_and_type_list() -> None:
    page = "<script type='application/ld+json'>" + json.dumps(
        {
            "@type": ["Recipe", "NewsArticle"],
            "name": "Toast",
            "totalTime": "PT5M",
            "recipeIngredient": ["bread"],
            "recipeInstructions": "Toast the bread.\nButter it.",
        }
    ) + "</script>"
    recipe = extract_recipe(page)
    assert recipe["steps"] == ["Toast the bread.", "Butter it."]
    assert recipe["total_minutes"] == 5 and "url" not in recipe


def test_pages_without_recipe_data_yield_nothing() -> None:
    assert extract_recipe("<html><body>Just a blog</body></html>") is None
    assert extract_recipe('<script type="application/ld+json">{not json</script>') is None
    assert extract_recipe('<script type="application/ld+json">{"@type":"Article"}</script>') is None


async def test_search_fetches_hits_and_keeps_structured_ones(monkeypatch) -> None:
    async def fake_search(query, limit):
        assert query == "fluffy pancakes recipe" and limit == 5
        return [
            {"title": "a", "url": "https://a.example/p", "snippet": ""},
            {"title": "b", "url": "https://b.example/blog", "snippet": ""},
            {"title": "c", "url": "https://c.example/down", "snippet": ""},
        ]

    async def fake_fetch(url, **kwargs):
        if url.endswith("/down"):
            raise recipes.WebSearchError("nope")
        if url.endswith("/blog"):
            return url, "<html>no data</html>"
        return url, PAGE

    monkeypatch.setattr(recipes, "web_search", fake_search)
    monkeypatch.setattr(recipes, "fetch_html", fake_fetch)
    found = await search_recipes("fluffy pancakes", 3)
    assert [item["title"] for item in found["recipes"]] == ["Fluffy Buttermilk Pancakes"]
    assert found["recipes"][0]["url"] == "https://a.example/p"
    assert len(found["searched"]) == 3
    with pytest.raises(ValueError):
        await search_recipes("   ")


MEAL = {
    "idMeal": "52795", "strMeal": "Chicken Handi", "strCategory": "Chicken", "strArea": "Indian",
    "strInstructions": "Heat the oil.\r\nAdd onions.\r\n\r\nSimmer.",
    "strSource": "https://example.com/handi",
    "strIngredient1": "Chicken", "strMeasure1": "1.2 kg", "strIngredient2": "Onion", "strMeasure2": "5 thinly sliced",
    "strIngredient3": "", "strMeasure3": " ",
}


async def test_falls_back_to_mealdb_when_search_is_throttled(monkeypatch) -> None:
    async def throttled(query, limit):
        raise recipes.WebSearchError("rate limited")

    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["s"])
        if request.url.params["s"] == "chicken":
            return httpx.Response(200, json={"meals": [MEAL]})
        return httpx.Response(200, json={"meals": None})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: original(*a, transport=httpx.MockTransport(handle), **k))
    monkeypatch.setattr(recipes, "web_search", throttled)
    found = await search_recipes("high protein chicken dinner", 3)
    assert found["source"] == "TheMealDB"
    assert asked == ["high protein chicken dinner", "chicken"]
    recipe = found["recipes"][0]
    assert recipe["title"] == "Chicken Handi" and recipe["cuisine"] == "Indian"
    assert recipe["ingredients"] == ["1.2 kg Chicken", "5 thinly sliced Onion"]
    assert recipe["step_count"] == 3 and "steps" not in recipe


async def test_throttled_search_and_empty_mealdb_is_an_error(monkeypatch) -> None:
    async def throttled(query, limit):
        raise recipes.WebSearchError("rate limited")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: original(*a, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"meals": None})), **k),
    )
    monkeypatch.setattr(recipes, "web_search", throttled)
    with pytest.raises(recipes.WebSearchError):
        await search_recipes("unicorn stew")


ROUNDUP = """<html><body><h1>15 High-Protein Dinners</h1>
<a href="https://a.example/recipes/">All recipes</a>
<a href="https://a.example/category/recipes/dinner/">Dinners</a>
<img src="x"><a href="https://a.example/wp-content/uploads/recipe-card.png">img</a>
<h2><a href="https://a.example/recipe/12345/chicken-bowls/">Chicken bowls</a></h2>
<a href="https://a.example/recipe/12345/chicken-bowls/#comments">comments</a>
<a href="https://a.example/recipe/678/salmon-rice/?utm=x">Salmon rice</a>
<a href="https://other.example/recipe/1/">elsewhere</a>
<a href="https://a.example/about/">About</a>
</body></html>"""


def test_roundup_links_point_at_recipes_on_the_same_site() -> None:
    assert recipe_links(ROUNDUP, "https://a.example/high-protein-dinners/") == [
        "https://a.example/recipe/12345/chicken-bowls/",
        "https://a.example/recipe/678/salmon-rice/",
    ]


def test_roundup_item_list_and_headings_beat_path_guessing() -> None:
    page = """<script type="application/ld+json">{"@type":"ItemList","itemListElement":[
      {"@type":"ListItem","position":1,"url":"https://b.example/one-pot-chicken/"},
      {"@type":"ListItem","position":2,"item":{"@id":"https://b.example/beef-chili/#recipe"}}]}</script>
    <h3><a href="https://b.example/lentil-soup/">Lentil soup</a></h3>
    <a href="https://b.example/category/recipes/">cat</a>"""
    assert recipe_links(page, "https://b.example/roundup/") == [
        "https://b.example/one-pot-chicken/",
        "https://b.example/beef-chili/",
        "https://b.example/lentil-soup/",
    ]


async def test_search_follows_roundup_links_for_generic_queries(monkeypatch) -> None:
    async def fake_search(query, limit):
        return [{"title": "15 dinners", "url": "https://a.example/high-protein-dinners/", "snippet": ""}]

    fetched: list[str] = []

    async def fake_fetch(url, **kwargs):
        fetched.append(url)
        if url.endswith("dinners/"):
            return url, ROUNDUP
        return url, PAGE.replace("Fluffy Buttermilk Pancakes", "Chicken bowls" if "chicken" in url else "Salmon rice")

    monkeypatch.setattr(recipes, "web_search", fake_search)
    monkeypatch.setattr(recipes, "fetch_html", fake_fetch)
    found = await search_recipes("high protein dinner for the family", 3)
    assert found["source"] == "web"
    assert [item["title"] for item in found["recipes"]] == ["Chicken bowls", "Salmon rice"]
    assert fetched == [
        "https://a.example/high-protein-dinners/",
        "https://a.example/recipe/12345/chicken-bowls/",
        "https://a.example/recipe/678/salmon-rice/",
    ]
    assert found["searched"] == fetched


def test_best_pick_prefers_title_match_rating_and_completeness() -> None:
    cake = {"title": "Fluffy Chocolate Cake", "rating": 4.6, "rating_count": 900, "ingredients": ["a"], "steps": ["b"], "total_minutes": 60}
    popular_but_wrong = {"title": "Vanilla Cupcakes", "rating": 4.9, "rating_count": 5000, "ingredients": ["a"], "steps": ["b"]}
    no_steps = {"title": "Fluffy Chocolate Cake with Frosting", "rating": 4.9, "rating_count": 9000, "ingredients": ["a"], "steps": []}
    frosting = {"title": "Not Too Sweet Chocolate Frosting", "rating": 4.9, "rating_count": 2000, "ingredients": ["a"], "steps": ["b"], "total_minutes": 15}
    query = "fluffy chocolate cake with light not too sweet chocolate frosting"
    assert pick_best([popular_but_wrong, no_steps, frosting, cake], query) is cake
    assert pick_best([], "x") is None
    from house_agent.recipes import dish_query
    assert dish_query(query) == "fluffy chocolate cake"
    assert dish_query("high protein dinner") is None


async def test_best_mode_returns_one_full_recipe(monkeypatch) -> None:
    async def fake_search(query, limit):
        return [
            {"title": "a", "url": "https://a.example/cake", "snippet": ""},
            {"title": "b", "url": "https://b.example/pancakes", "snippet": ""},
        ]

    async def fake_fetch(url, **kwargs):
        if url.endswith("/cake"):
            return url, PAGE.replace("Fluffy Buttermilk Pancakes", "Fluffy Chocolate Cake")
        return url, PAGE

    monkeypatch.setattr(recipes, "web_search", fake_search)
    monkeypatch.setattr(recipes, "fetch_html", fake_fetch)
    found = await search_recipes("fluffy chocolate cake", best=True)
    assert "recipes" not in found
    assert found["recipe"]["title"] == "Fluffy Chocolate Cake"
    assert found["recipe"]["steps"] == ["Whisk the dry ingredients.", "Stir in buttermilk and eggs.", "Cook on a hot griddle."]
    assert found["considered"] == ["Fluffy Buttermilk Pancakes"]
