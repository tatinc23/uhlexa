def test_glued_sentences_get_a_space_but_normal_text_is_untouched() -> None:
    from app.providers.keiki.gateway import _space_joined_sentences as fix

    assert (
        fix("Pulling that up for you.Checked your family board") == "Pulling that up for you. Checked your family board"
    )
    assert fix("Done!Candles and ice are on the list.") == "Done! Candles and ice are on the list."
    assert fix("It is at node.js today. Next, Dr. Smith.") == "It is at node.js today. Next, Dr. Smith."
    assert fix("The U.S.A. list") == "The U.S.A. list"
